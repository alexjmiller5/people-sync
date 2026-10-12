"""`people-sync port google`: give every person known only from an Apple card a Google
contact. Apple Contacts is the inbox, Google the address book.

Candidates are live people with an `apple_contacts` account and no `google_contacts`
one. Each linked Apple card is read from the address books (this Mac's and, with
PEOPLE_SYNC_ADDRESSBOOK_HOST, another Mac's over ssh) and held in memory only: phone
numbers, emails and the birthday never reach soma, a log, a capture or stdout. The name
written is the reviewed one in `people`, never the Apple name fields, which often carry
context ("X's Friend", a role). Org, notes, nickname, addresses and the photo stay out
of Google and are listed as skipped.

Dry run by default. `--apply` creates one contact per People API batch call (gog
documents no input/output order inside a batch), records `google_contacts:<resource>`
in the ledger right away (pending, raw = the Apple record ids it was ported from, with
an imported_from edge from each), reads the contact back, and links it through
`reconcile link` only when the read-back matches. A rerun resumes a recorded but
unlinked port instead of creating a duplicate. The Apple card is never touched.
"""

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field

from people_sync import comm, reconcile, somadata

CANDIDATES_SQL = """
SELECT p.id, p.name, p.first_name, p.middle_name, p.last_name, a.source_id AS apple_id
FROM people p
JOIN person_accounts a ON a.person_id = p.id AND a.platform = 'apple_contacts'
  AND a.deleted_at IS NULL AND a.source_id IS NOT NULL
WHERE p.deleted_at IS NULL
  AND NOT EXISTS (SELECT 1 FROM person_accounts g WHERE g.person_id = p.id
                  AND g.platform = 'google_contacts' AND g.deleted_at IS NULL)
ORDER BY p.name, p.id, a.source_id
"""

_APPLE_ID = re.compile(r"[0-9A-Fa-f]{8}(?:-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}:ABPerson")
_DATE = "r.ZBIRTHDAY + 978307200, 'unixepoch', 'localtime'"
# Values leave SQLite only for what is written or previewed; a note only as a presence flag.
_CARD_SQL = f"""
SELECT r.ZUNIQUEID AS apple, r.ZFIRSTNAME AS first, r.ZMIDDLENAME AS middle,
  r.ZLASTNAME AS last,
  CASE WHEN r.ZBIRTHDAY IS NULL THEN NULL
       WHEN strftime('%Y', {_DATE}) < '1900' THEN strftime('--%m-%d', {_DATE})
       ELSE date({_DATE}) END AS birthday,
  (SELECT json_group_array(json_object('value', v, 'label', l)) FROM
     (SELECT ZFULLNUMBER AS v, ZLABEL AS l FROM ZABCDPHONENUMBER
      WHERE ZOWNER = r.Z_PK AND ZFULLNUMBER <> '' ORDER BY ZORDERINGINDEX)) AS phones,
  (SELECT json_group_array(json_object('value', v, 'label', l)) FROM
     (SELECT ZADDRESS AS v, ZLABEL AS l FROM ZABCDEMAILADDRESS
      WHERE ZOWNER = r.Z_PK AND ZADDRESS <> '' ORDER BY ZORDERINGINDEX)) AS emails,
  r.ZORGANIZATION AS org, r.ZJOBTITLE AS title, r.ZNICKNAME AS nick,
  EXISTS (SELECT 1 FROM ZABCDNOTE WHERE ZCONTACT = r.Z_PK AND COALESCE(ZTEXT, '') <> '')
    AS has_note,
  (SELECT count(*) FROM ZABCDPOSTALADDRESS WHERE ZOWNER = r.Z_PK) AS addresses,
  (r.ZIMAGEDATA IS NOT NULL OR r.ZTHUMBNAILIMAGEDATA IS NOT NULL) AS photo
FROM ZABCDRECORD r WHERE r.ZUNIQUEID IN ({{ids}})
"""

# Apple's stored phone labels -> People API phone types. iPhone is a mobile by name.
PHONE_TYPES = {
    "_$!<Mobile>!$_": "mobile",
    "iPhone": "mobile",
    "_$!<Home>!$_": "home",
    "_$!<Work>!$_": "work",
    "_$!<Main>!$_": "main",
    "_$!<HomeFAX>!$_": "homeFax",
    "_$!<WorkFAX>!$_": "workFax",
    "_$!<OtherFAX>!$_": "otherFax",
    "_$!<Pager>!$_": "pager",
    "_$!<Other>!$_": "other",
}
EMAIL_TYPES = {"_$!<Home>!$_": "home", "_$!<Work>!$_": "work", "_$!<Other>!$_": "other"}
NAME_KEYS = (
    ("first_name", "givenName"),
    ("middle_name", "middleName"),
    ("last_name", "familyName"),
)
ASSERTED_BY = "script:people-sync port google"


class GogError(RuntimeError):
    def __init__(self, what: str, resource: str | None = None):
        self.resource = resource
        super().__init__(what + (f" (it may have created {resource})" if resource else ""))


def phone_type(label: str | None) -> tuple[str, bool]:
    """(People API type, assumed): an unknown label on a person's number is a mobile."""
    known = PHONE_TYPES.get(label or "")
    return (known, False) if known else ("mobile", True)


_INVISIBLE = re.compile(r"[\u200e\u200f\u202a-\u202e\u2066-\u2069]")


def _clean(value: str) -> str:
    """Obvious noise only: surrounding whitespace and invisible direction marks."""
    return _INVISIBLE.sub("", value).strip()


def _phone_key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    return digits[1:] if len(digits) == 11 and digits.startswith("1") else digits


def read_cards(apple_ids: list[str]) -> tuple[list[dict], list[str]]:
    ids = [i for i in apple_ids if _APPLE_ID.fullmatch(i)]
    if not ids:
        return [], []
    return comm.read_address_books(_CARD_SQL.format(ids=",".join(f"'{i}'" for i in ids)))


def name_parts(person: dict, cards: list[dict]) -> tuple[dict, list[str]]:
    """The reviewed People name as People API name parts, plus what to flag."""
    flags = []
    parts = {key: person[col] for col, key in NAME_KEYS if person.get(col)}
    if not parts:
        words = (person.get("name") or "").split()
        parts = {"givenName": words[0]} if words else {}
        if len(words) > 1:
            parts = {"givenName": words[0], "familyName": " ".join(words[1:])}
            flags.append("People has no name parts; split from its name as given/family")
    spelled = " ".join(parts[k] for _, k in NAME_KEYS if parts.get(k))
    if spelled != (person.get("name") or ""):
        flags.append(f"name parts {spelled!r} do not spell the People name {person.get('name')!r}")
    for card in cards:
        apple = [card.get(k) or None for k in ("first", "middle", "last")]
        shown = " ".join(p for p in apple if p)
        flag = f"Apple card name {shown!r} differs from People; People's is written"
        if apple != [parts.get(k) for _, k in NAME_KEYS] and flag not in flags:
            flags.append(flag)
    return parts, flags


@dataclass
class Plan:
    person_id: str
    name: str
    apple_ids: list[str]
    body: dict = field(default_factory=dict)
    phone_labels: list[str] = field(default_factory=list)
    photo: bool = False
    flags: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    skip: str | None = None

    @property
    def assumed(self) -> int:
        return self.phone_labels.count("assumed mobile")

    @property
    def record_ids(self) -> list[str]:
        return ["apple_contacts:" + i for i in self.apple_ids]


def _birthday(values: list[str]) -> tuple[dict | None, bool]:
    """(People API date, conflict) from `YYYY-MM-DD` / `--MM-DD` values. A yearless
    value agrees with a dated one on the same day: an unknown year is not a conflict."""
    days = {v[-5:] for v in values}
    years = {v[:4] for v in values if not v.startswith("--")}
    if len(days) > 1 or len(years) > 1:
        return None, True
    if not values:
        return None, False
    month, day = (int(x) for x in days.pop().split("-"))
    return ({"year": int(years.pop())} if years else {}) | {"month": month, "day": day}, False


def _distinct(cards: list[dict], *keys: str) -> list[str]:
    out = []
    for card in cards:
        for key in keys:
            value = _clean(card.get(key) or "")
            if value and value not in out:
                out.append(value)
    return out


def plan_person(rows: list[dict], cards: list[dict], books: list[str] = ()) -> Plan:
    """One person's contact body from every linked card, distinct values merged."""
    person = rows[0]
    apple_ids = [r["apple_id"] for r in rows]
    plan = Plan(person["id"], person["name"], apple_ids)
    found = [c for c in cards if c["apple"] in apple_ids]
    missing = len(set(apple_ids) - {c["apple"] for c in found})
    if missing:
        read = ", ".join(books) or "none"
        plan.skip = f"{missing} of {len(apple_ids)} Apple cards not in the books read ({read})"
        return plan
    parts, plan.flags = name_parts(person, found)
    phones: dict[str, dict] = {}
    emails: dict[str, dict] = {}
    for card in found:
        for item in json.loads(card.get("phones") or "[]"):
            value = _clean(item["value"])
            kind, assumed = phone_type(item.get("label"))
            old = phones.setdefault(
                _phone_key(value), {"value": value, "type": kind, "assumed": assumed}
            )
            if old["assumed"] and not assumed:
                old.update(type=kind, assumed=False)
            if value.startswith("+") and not old["value"].startswith("+"):
                old["value"] = value
        for item in json.loads(card.get("emails") or "[]"):
            value = _clean(item["value"])
            entry = emails.setdefault(value.casefold(), {"value": value})
            kind = EMAIL_TYPES.get(item.get("label") or "")
            if kind and "type" not in entry:
                entry["type"] = kind
    date, conflict = _birthday([c["birthday"] for c in found if c.get("birthday")])
    if not (phones or emails or date or conflict):
        plan.skip = "nothing to port: the Apple card has no phone, email or birthday"
        return plan
    plan.body = {"names": [parts]}
    if phones:
        plan.body["phoneNumbers"] = [
            {"value": p["value"], "type": p["type"]} for p in phones.values()
        ]
        plan.phone_labels = [
            "assumed mobile" if p["assumed"] else p["type"] for p in phones.values()
        ]
    if emails:
        plan.body["emailAddresses"] = list(emails.values())
    if date:
        plan.body["birthdays"] = [{"date": date}]
    if conflict:
        plan.flags.append("the Apple cards disagree on the birthday; none is written")
    plan.photo = any(c.get("photo") for c in found)
    org = _distinct(found, "org", "title")
    if org:
        plan.skipped.append(f"org/title {', '.join(map(repr, org))} (context stays in People)")
    nick = _distinct(found, "nick")
    if nick:
        plan.skipped.append(f"nickname {', '.join(map(repr, nick))} (People holds nicknames)")
    if any(c.get("has_note") for c in found):
        plan.skipped.append("note (context stays in People)")
    addresses = sum(c.get("addresses") or 0 for c in found)
    if addresses:
        noun = "address" if addresses == 1 else "addresses"
        plan.skipped.append(f"{addresses} postal {noun} (not ported; add in Google by hand)")
    if plan.photo:
        plan.skipped.append("photo (not ported; add in Google by hand)")
    return plan


def _gog(args: list[str], stdin: str | None = None) -> str:
    proc = subprocess.run(
        ["gog", *args, "-j", "--no-input", "--gmail-no-send"],
        input=stdin,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        # The output can hold contact values: only a resource name leaves it.
        found = re.search(r"people/c[0-9A-Za-z_-]+", proc.stdout or "")
        raise GogError("gog " + " ".join(args[:3]) + " failed", found and found.group(0))
    return proc.stdout


def create(body: dict) -> str:
    out = json.loads(_gog(["contacts", "batch", "create", "--from-file", "-"], json.dumps([body])))
    batch = (out.get("batches") or [{}])[0]
    created = (batch.get("response") or {}).get("createdPeople") or []
    resource = ((created[0] if created else {}).get("person") or {}).get("resourceName")
    if out.get("completed") != 1 or batch.get("state") != "completed" or not resource:
        raise GogError("contacts batch create was not confirmed", resource)
    return resource


def verify(body: dict, person: dict) -> list[str]:
    """What the read-back contact lacks, named by field only - never by value."""
    problems = []
    name = (person.get("names") or [{}])[0]
    if any(name.get(k) != body["names"][0].get(k) for _, k in NAME_KEYS):
        problems.append("name")

    def types(p):
        return sorted(x.get("type") for x in p.get("phoneNumbers") or [])

    if len(person.get("phoneNumbers") or []) != len(body.get("phoneNumbers") or []):
        problems.append("phone count")
    elif types(person) != types(body):
        problems.append("phone types")

    def addresses(p):
        return sorted(x["value"].casefold() for x in p.get("emailAddresses") or [])

    if addresses(person) != addresses(body):
        problems.append("emails")
    got = [b.get("date") for b in person.get("birthdays") or []]
    if got != [b["date"] for b in body.get("birthdays") or []]:
        problems.append("birthday")
    return problems


def _record_port(plan: Plan, resource: str) -> str:
    record_id = f"google_contacts:{resource}"
    now = somadata.now_iso()
    somadata.insert(
        "people_sync_records",
        [
            {
                "id": record_id,
                "source": "google_contacts",
                "source_id": resource,
                "handle": None,
                "name": " ".join(
                    plan.body["names"][0][k] for _, k in NAME_KEYS if plan.body["names"][0].get(k)
                ),
                "raw": json.dumps({"ported_from": plan.record_ids}),
                "follows_me": None,
                "i_follow": None,
                "status": "pending",
                "person_id": None,
                "suggested_person_id": None,
                "first_seen": now,
                "last_seen": now,
            }
        ],
    )
    return record_id


def _edges(plan: Plan, record_id: str) -> None:
    """An imported_from edge from each Apple record, inserted only where missing."""
    rows = {
        f"people_sync_records:{apple}:people_sync_records:{record_id}": apple
        for apple in plan.record_ids
    }
    ids = ",".join(somadata.sq(i) for i in rows)
    have = {r["id"] for r in somadata.sql(f"SELECT id FROM provenance WHERE id IN ({ids})")}
    missing = [
        {
            "id": edge_id,
            "from_kind": "people_sync_records",
            "from_ref": apple,
            "to_kind": "people_sync_records",
            "to_ref": record_id,
            "rel": "imported_from",
            "field": None,
            "detail": json.dumps(
                {"cue": "Google contact created from this Apple card", "confidence": "high"}
            ),
            "asserted_by": os.environ.get("PEOPLE_SYNC_ASSERTED_BY") or ASSERTED_BY,
        }
        for edge_id, apple in rows.items()
        if edge_id not in have
    ]
    if missing:
        somadata.insert("provenance", missing)


def _resumable() -> dict[str, dict]:
    """{Apple record id: a pending google_contacts record ported from it}."""
    out = {}
    for row in somadata.sql(
        "SELECT id, source_id, status, raw FROM people_sync_records "
        "WHERE source = 'google_contacts' AND status = 'pending' AND deleted_at IS NULL "
        "AND raw LIKE '%ported_from%'"
    ):
        for apple in json.loads(row["raw"] or "{}").get("ported_from") or []:
            out[apple] = row
    return out


def _show(plan: Plan) -> None:
    print(f"PORT {plan.name} ({plan.person_id}), {len(plan.apple_ids)} Apple card(s)")
    name = plan.body["names"][0]
    print("  name: " + " / ".join(f"{k.removesuffix('Name')} {v!r}" for k, v in name.items()))
    labels = f" ({', '.join(plan.phone_labels)})" if plan.phone_labels else ""
    print(f"  phones: {len(plan.phone_labels)}{labels}")
    print(f"  emails: {len(plan.body.get('emailAddresses') or [])}")
    print(f"  birthday: {'yes' if plan.body.get('birthdays') else 'no'}")
    print(f"  photo: {'yes' if plan.photo else 'no'}")
    for flag in plan.flags:
        print(f"  flag: {flag}")
    for what in plan.skipped:
        print(f"  skip: {what}")


def _apply(plan: Plan, resume: dict | None) -> None:
    if resume:
        resource, record_id = resume["source_id"], resume["id"]
        print(f"  resuming {resource}: created earlier, not linked yet")
    else:
        resource = create(plan.body)
        try:
            record_id = _record_port(plan, resource)
        except Exception as exc:
            sys.exit(
                f"created {resource} for {plan.person_id} but could not record it "
                f"({type(exc).__name__}); link it with `people-sync reconcile link` after "
                "`people-sync ingest google`, never by porting again"
            )
    try:
        _edges(plan, record_id)
    except RuntimeError as exc:
        print(f"  NOT LINKED {resource}: provenance not recorded ({type(exc).__name__}); rerun")
        return
    problems = verify(plan.body, json.loads(_gog(["contacts", "raw", resource])))
    if problems:
        print(f"  VERIFY FAILED {resource}: {', '.join(problems)} differ; left pending, not linked")
        return
    print(f"  CREATED {resource}: read back and verified")
    reconcile.link(plan.person_id, record_id, False, reconcile.Ops(apply=True))


def run(people: list[str], apply: bool) -> None:
    rows = somadata.sql(CANDIDATES_SQL)
    if people:
        rows = [r for r in rows if r["id"] in people]
    by_person: dict[str, list[dict]] = {}
    for row in rows:
        by_person.setdefault(row["id"], []).append(row)
    cards, books = read_cards([r["apple_id"] for r in rows])
    print(f"address books read: {', '.join(books) or 'none'}")
    resumable = _resumable() if by_person else {}
    ported = skipped = 0
    for person_rows in by_person.values():
        plan = plan_person(person_rows, cards, books)
        if plan.skip:
            skipped += 1
            print(f"SKIP {plan.name} ({plan.person_id}): {plan.skip}")
            continue
        ported += 1
        _show(plan)
        resume = next((resumable[r] for r in plan.record_ids if r in resumable), None)
        if apply:
            try:
                _apply(plan, resume)
            except GogError as exc:
                sys.exit(f"{plan.name} ({plan.person_id}): {exc}; inspect Google before rerunning")
        elif resume:
            print(
                f"  resume: {resume['source_id']} was created earlier; --apply verifies and links it"
            )
    verb = "ported" if apply else "to port"
    print(f"\n{ported} {verb}, {skipped} skipped")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="people-sync port",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", choices=("google",), help="the address book to port into")
    parser.add_argument(
        "--person", action="append", default=[], help="limit to a people.id (repeatable)"
    )
    parser.add_argument(
        "--apply", action="store_true", help="create, verify and link (default: preview)"
    )
    args = parser.parse_args(argv)
    run(args.person, args.apply)
