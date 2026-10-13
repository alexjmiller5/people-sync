"""People is the contact record; Google contacts are a synced copy. `people-sync pull google`
reads every linked Google contact and compares the fields People owns (names, phones,
emails, postal addresses, birthday) with soma.

Every contact carries a review mark in its `clientData` (`people-sync.contact` = the
fingerprint of its contact fields as last reviewed). Compared with it, a contact is
`new` (never pulled), `unchanged` (Google as reviewed: any difference is soma's newer
state, which `push google` writes) or `changed` (edited in Google since). Items:

- add: a Google value soma lacks (a number, an email, a current address, name parts that
  spell the People name, a birthday or its year) on a new or changed contact; `--apply`
  writes it to soma with an evidence edge from the Google ledger record.
- missing: a value soma holds that a changed contact lost (deleted in Google?).
- conflict: a different name or birthday on a new or changed contact.

Missing and conflict items wait for the user: `--accept decisions.json` maps an item id to
`google` (take Google's side into soma) or `soma` (keep soma; push restores Google). A
contact with nothing left open gets the mark of its current Google fields, and
`push google --field contact` then overwrites only marked, unchanged contacts.
Dry run by default; `--output` saves the items as JSON for a review page.
"""

import argparse
import hashlib
import json

from people_sync import port, push, somadata

MARK_KEY = "people-sync.contact"
FIELDS = "names,phoneNumbers,emailAddresses,addresses,birthdays,clientData"
LINKS_SQL = """
SELECT p.id, p.name, p.first_name, p.middle_name, p.last_name, p.birthday,
  g.source_id AS google_id
FROM people p
JOIN person_accounts g ON g.person_id = p.id AND g.platform = 'google_contacts'
  AND g.deleted_at IS NULL AND g.source_id IS NOT NULL
WHERE p.deleted_at IS NULL
ORDER BY p.name, p.id, g.source_id
"""
PHONES_SQL = (
    "SELECT id, person_id, number, label, is_primary FROM person_phones WHERE deleted_at IS NULL"
)
EMAILS_SQL = (
    "SELECT id, person_id, address, label, is_primary FROM person_emails WHERE deleted_at IS NULL"
)
ADDRESSES_SQL = (
    "SELECT id, person_id, street, extended, city, region, postal_code, country, label "
    'FROM person_locations WHERE deleted_at IS NULL AND street IS NOT NULL AND "end" IS NULL'
)
ADDRESS_KEYS = (
    ("street", "streetAddress"),
    ("extended", "extendedAddress"),
    ("city", "city"),
    ("region", "region"),
    ("postal_code", "postalCode"),
    ("country", "country"),
)
NAME_KEYS = (
    ("given", "givenName", "first_name"),
    ("middle", "middleName", "middle_name"),
    ("family", "familyName", "last_name"),
)


def _text(value) -> str | None:
    value = port._clean(value or "")
    return value or None


def phone_key(number: str) -> str:
    return port._phone_key(number)


def address_key(a: dict) -> str:
    return "|".join(
        " ".join((a.get(k) or "").casefold().split()) for k in ("street", "city", "postal_code")
    )


def _birthday(date: dict | None) -> str | None:
    if not date or not date.get("month") or not date.get("day"):
        return None
    # a year before 1900 is the Contacts placeholder for "no year" (1604)
    year = f"{date['year']:04d}" if (date.get("year") or 0) >= 1900 else "-"
    return f"{year}-{date['month']:02d}-{date['day']:02d}"


def google_state(person: dict) -> dict:
    """The contact fields People owns, as Google holds them, keyed the way they compare."""
    name = (person.get("names") or [{}])[0]
    phones = {}
    for p in person.get("phoneNumbers") or []:
        number = _text(p.get("canonicalForm")) or _text(p.get("value"))
        if number:
            phones.setdefault(phone_key(number), {"number": number, "label": _text(p.get("type"))})
    emails = {}
    for e in person.get("emailAddresses") or []:
        address = _text(e.get("value"))
        if address:
            emails.setdefault(
                address.casefold(), {"address": address, "label": _text(e.get("type"))}
            )
    addresses = {}
    for a in person.get("addresses") or []:
        row = {ours: _text(a.get(theirs)) for ours, theirs in ADDRESS_KEYS} | {
            "label": _text(a.get("type"))
        }
        if row["street"] or row["city"]:
            addresses.setdefault(address_key(row), row)
    birthdays = [_birthday(b.get("date")) for b in person.get("birthdays") or []]
    return {
        "names": {ours: _text(name.get(theirs)) for ours, theirs, _ in NAME_KEYS},
        "phones": phones,
        "emails": emails,
        "addresses": addresses,
        "birthday": next((b for b in birthdays if b), None),
    }


def soma_state(person: dict, phones: list, emails: list, addresses: list) -> dict:
    """The same fields from soma rows, primary values first."""
    first = sorted(phones, key=lambda r: -(r.get("is_primary") or 0))
    first_emails = sorted(emails, key=lambda r: -(r.get("is_primary") or 0))
    return {
        "names": {ours: _text(person.get(col)) for ours, _, col in NAME_KEYS},
        "name": person.get("name"),
        "phones": {
            phone_key(r["number"]): {"number": r["number"], "label": r.get("label")} for r in first
        },
        "emails": {
            r["address"].casefold(): {"address": r["address"], "label": r.get("label")}
            for r in first_emails
        },
        "addresses": {
            address_key(r): {k: r.get(k) for k, _ in ADDRESS_KEYS} | {"label": r.get("label")}
            for r in addresses
        },
        "birthday": _plain_birthday(person.get("birthday")),
    }


def _plain_birthday(value: str | None) -> str | None:
    """A year before 1900 is a placeholder for "no year" (0001, 1604)."""
    if value and not value.startswith("-") and int(value[:4]) < 1900:
        return "-" + value[4:]
    return value


def fingerprint(state: dict) -> str:
    """Equal when the owned fields are the same; number and email formatting do not count."""
    owned = {k: state[k] for k in ("names", "addresses", "birthday")} | {
        k: {key: v["label"] for key, v in state[k].items()} for k in ("phones", "emails")
    }
    return hashlib.sha256(json.dumps(owned, sort_keys=True).encode()).hexdigest()


def _spelled(names: dict) -> str:
    return " ".join(
        v
        for _, v in sorted(names.items(), key=lambda kv: ["given", "middle", "family"].index(kv[0]))
        if v
    )


def _birthday_items(soma: str | None, google: str | None) -> list[tuple[str, str | None]]:
    if not google or soma == google:
        return []
    if not soma:
        return [("add", google)]
    if soma[-5:] == google[-5:] and "-" in (soma[0], google[0]):
        # an unknown year is not a conflicting year; Google's year fills a yearless one
        return [("add", google)] if soma.startswith("-") else []
    return [("conflict", google)]


def diff(soma: dict, google: dict, status: str) -> list[dict]:
    """Items for one contact; `status` is new / unchanged / changed against its mark."""
    if status == "unchanged":
        return []
    items = []

    def item(kind, field, key=None, g=None, s=None):
        items.append({"kind": kind, "field": field, "key": key, "google": g, "soma": s})

    g_names, s_names = google["names"], soma["names"]
    if any(g_names.values()) and g_names != s_names:
        if not any(s_names.values()) and _spelled(g_names) == (soma.get("name") or ""):
            item("add", "names", g=g_names)
        else:
            item(
                "conflict",
                "names",
                g=g_names,
                s=s_names if any(s_names.values()) else soma.get("name"),
            )
    for kind, value in _birthday_items(soma["birthday"], google["birthday"]):
        item(kind, "birthday", g=value, s=soma["birthday"])
    for field in ("phones", "emails", "addresses"):
        for key, value in google[field].items():
            if key not in soma[field]:
                item("add", field, key, g=value)
        if status == "changed":
            for key, value in soma[field].items():
                if key not in google[field]:
                    item("missing", field, key, s=value)
    return items


def item_id(resource: str, it: dict) -> str:
    return f"{resource}:{it['field']}" + (f":{it['key']}" if it["key"] else "")


def to_google(state: dict) -> dict:
    """People API fields for a soma state; masks names, phones, emails, addresses, birthdays."""
    names = {theirs: state["names"][ours] for ours, theirs, _ in NAME_KEYS if state["names"][ours]}
    if not names and state.get("name"):
        names = {"unstructuredName": state["name"]}
    date = None
    if state["birthday"]:
        y, m, d = (
            state["birthday"].rsplit("-", 2)
            if not state["birthday"].startswith("--")
            else ("", *state["birthday"][2:].split("-"))
        )
        date = {"month": int(m), "day": int(d)} | ({"year": int(y)} if y and int(y) >= 1900 else {})
    return {
        "names": [names] if names else [],
        "phoneNumbers": [
            {"value": v["number"]} | ({"type": v["label"]} if v["label"] else {})
            for v in state["phones"].values()
        ],
        "emailAddresses": [
            {"value": v["address"]} | ({"type": v["label"]} if v["label"] else {})
            for v in state["emails"].values()
        ],
        "addresses": [
            {theirs: v[ours] for ours, theirs in ADDRESS_KEYS if v.get(ours)}
            | ({"type": v["label"]} if v.get("label") else {})
            for v in state["addresses"].values()
        ],
        "birthdays": [{"date": date}] if date else [],
    }


def soma_states(rows: list[dict]) -> dict[str, dict]:
    """{person id: soma state} for the given people rows."""
    by = {}
    for sql, kind in ((PHONES_SQL, "phones"), (EMAILS_SQL, "emails"), (ADDRESSES_SQL, "addresses")):
        for r in somadata.sql(sql):
            by.setdefault(r["person_id"], {}).setdefault(kind, []).append(r)
    return {
        r["id"]: soma_state(
            r, *(by.get(r["id"], {}).get(k, []) for k in ("phones", "emails", "addresses"))
        )
        for r in rows
    }


def status_of(contact: dict) -> str:
    mark = push._client(contact).get(MARK_KEY)
    if not mark:
        return "new"
    return "unchanged" if mark == fingerprint(google_state(contact)) else "changed"


def _edge(resource: str, to_kind: str, to_ref: str, field: str | None, cue: str) -> dict:
    return {
        "id": f"people_sync_records:google_contacts:{resource}:{to_kind}:{to_ref}:{field or ''}",
        "from_kind": "people_sync_records",
        "from_ref": f"google_contacts:{resource}",
        "to_kind": to_kind,
        "to_ref": to_ref,
        "rel": "evidence_of",
        "field": field,
        "detail": json.dumps({"cue": cue, "confidence": "high"}),
        "asserted_by": "script:people-sync pull google",
    }


class Writes:
    """soma writes collected across contacts, flushed in a few statements."""

    def __init__(self):
        self.rows: dict[str, list[dict]] = {
            "person_phones": [],
            "person_emails": [],
            "person_locations": [],
        }
        self.people: dict[str, dict] = {}
        self.retire: list[str] = []
        self.edges: list[dict] = []

    def take(self, pid: str, resource: str, it: dict, today: str) -> None:
        f, g = it["field"], it["google"]
        if it["kind"] == "missing":
            if f == "addresses":
                self.retire.append(
                    f'UPDATE person_locations SET "end" = {somadata.sq(today)} WHERE id = {somadata.sq(it["soma_id"])}'
                )
            else:
                table = "person_" + f
                self.retire.append(
                    f"UPDATE {table} SET deleted_at = updated_at WHERE id = {somadata.sq(it['soma_id'])}"
                )
            return
        if f == "names":
            self.people.setdefault(pid, {}).update(
                {col: g[ours] for ours, _, col in NAME_KEYS if g[ours]}
            )
            if it["kind"] == "conflict":
                self.people[pid]["name"] = _spelled(g)
            self.edges.append(_edge(resource, "people", pid, "first_name", "Google contact name"))
        elif f == "birthday":
            self.people.setdefault(pid, {})["birthday"] = g
            self.edges.append(_edge(resource, "people", pid, "birthday", "Google contact birthday"))
        elif f == "phones":
            rid = f"{pid}:{it['key']}"
            if not any(r["id"] == rid for r in self.rows["person_phones"]):
                self.rows["person_phones"].append(
                    {
                        "id": rid,
                        "person_id": pid,
                        "number": g["number"],
                        "label": g["label"],
                        "is_primary": int(
                            not any(r["person_id"] == pid for r in self.rows["person_phones"])
                        ),
                    }
                )
                self.edges.append(
                    _edge(resource, "person_phones", rid, None, "Google contact phone number")
                )
        elif f == "emails":
            rid = f"{pid}:{it['key']}"
            if not any(r["id"] == rid for r in self.rows["person_emails"]):
                self.rows["person_emails"].append(
                    {
                        "id": rid,
                        "person_id": pid,
                        "address": g["address"],
                        "label": g["label"],
                        "is_primary": int(
                            not any(r["person_id"] == pid for r in self.rows["person_emails"])
                        ),
                    }
                )
                self.edges.append(
                    _edge(resource, "person_emails", rid, None, "Google contact email address")
                )
        elif f == "addresses":
            rid = f"addr:{pid}:{hashlib.sha256(it['key'].encode()).hexdigest()[:16]}"
            if not any(r["id"] == rid for r in self.rows["person_locations"]):
                self.rows["person_locations"].append(
                    {
                        "id": rid,
                        "person_id": pid,
                        **{k: g.get(k) for k, _ in ADDRESS_KEYS},
                        "label": g.get("label"),
                        "source": "google_contacts",
                    }
                )
                self.edges.append(
                    _edge(resource, "person_locations", rid, None, "Google contact postal address")
                )

    def flush(self) -> None:
        for table, rows in self.rows.items():
            if rows:
                somadata.insert(table, rows)
        for pid, values in self.people.items():
            sets = ", ".join(f"{col} = {somadata.sq(v)}" for col, v in values.items())
            somadata.sql(f"UPDATE people SET {sets} WHERE id = {somadata.sq(pid)}")
        for statement in self.retire:
            somadata.sql(statement)
        if self.edges:
            somadata.insert("provenance", self.edges)


def _value(value) -> str | None:
    if not isinstance(value, dict):
        return value
    if "given" in value:
        return _spelled(value)
    return (
        value.get("number")
        or value.get("address")
        or ", ".join(v for k, v in value.items() if v and k != "label")
    )


def _show(item: dict) -> str:
    value = _value(item["google"] if item["kind"] != "missing" else item["soma"])
    soma = f" (soma: {_value(item['soma'])})" if item["kind"] == "conflict" else ""
    return f"{item['kind']} {item['field']}: {value}{soma}"


def run(people: list[str], apply: bool, accept: dict[str, str], output: str | None) -> None:
    rows = somadata.sql(LINKS_SQL)
    if people:
        rows = [r for r in rows if r["id"] in people]
    states = soma_states(rows)
    contacts = push.read(list(dict.fromkeys(r["google_id"] for r in rows)))
    today = somadata.now_iso()[:10]
    writes, marks, report = Writes(), [], []
    counts = {"new": 0, "changed": 0, "unchanged": 0, "open": 0}
    for row in rows:
        pid, resource = row["id"], row["google_id"]
        c = contacts.get(resource)
        if not isinstance(c, dict):
            print(f"SKIP {row['name']} ({pid}) -> {resource}: Google did not return it ({c})")
            continue
        status = status_of(c)
        counts[status] += 1
        g = google_state(c)
        items = diff(states[pid], g, status)
        for it in items:
            it["id"] = item_id(resource, it)
            if it["kind"] == "missing":
                it["soma_id"] = _soma_row_id(pid, it)
        open_items = [it for it in items if it["kind"] != "add" and it["id"] not in accept]
        report.append(
            {
                "person_id": pid,
                "name": row["name"],
                "resource": resource,
                "status": status,
                "items": items,
            }
        )
        if items:
            print(f"PULL {row['name']} ({pid}) <- {resource} [{status}]")
            for it in items:
                decided = accept.get(it["id"])
                print(f"  {_show(it)}" + (f" -> {decided}" if decided else "") + f"  [{it['id']}]")
        if open_items:
            counts["open"] += 1
        if not apply:
            continue
        for it in items:
            if it["kind"] == "add" or accept.get(it["id"]) == "google":
                writes.take(pid, resource, it, today)
        if not open_items and status != "unchanged":
            marks.append({"resource": resource, "client": {MARK_KEY: fingerprint(g)}})
    if output:
        with open(output, "w") as f:
            json.dump(
                {"contacts": [r for r in report if r["items"]]}, f, indent=1, ensure_ascii=False
            )
    if apply:
        writes.flush()
        push._update(marks, contacts)
    verb = "marked reviewed" if apply else "to mark"
    print(
        f"\n{counts['new']} new, {counts['changed']} changed, {counts['unchanged']} unchanged; "
        f"{counts['open']} with open decisions; {len(marks) if apply else '-'} {verb}"
    )


def _soma_row_id(pid: str, it: dict) -> str:
    if it["field"] in ("phones", "emails"):
        return f"{pid}:{it['key']}"
    for r in somadata.sql(ADDRESSES_SQL):
        if r["person_id"] == pid and address_key(r) == it["key"]:
            return r["id"]
    return ""


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="people-sync pull",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", choices=("google",), help="the address book to read")
    parser.add_argument(
        "--person", action="append", default=[], help="limit to a people.id (repeatable)"
    )
    parser.add_argument("--accept", help="JSON {item id: google|soma} for missing/conflict items")
    parser.add_argument("--output", help="write the items as JSON (for a review page)")
    parser.add_argument(
        "--apply", action="store_true", help="write adds and decisions, mark reviewed contacts"
    )
    args = parser.parse_args(argv)
    accept = json.load(open(args.accept)) if args.accept else {}
    bad = {k: v for k, v in accept.items() if v not in ("google", "soma")}
    if bad:
        parser.error(f"decisions must be 'google' or 'soma': {sorted(bad)[:3]}")
    run(args.person, args.apply, accept, args.output)
