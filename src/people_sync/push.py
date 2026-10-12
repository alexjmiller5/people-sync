"""`people-sync push google`: People owns two fields of every linked Google contact and
writes them one way.

- **Company** = how the user knows the person: their `people.circles`, in order, joined
  with ", " (no circles = no company). A Google company or title that People does not
  hold (not a circle, an employment, or a value this command wrote) blocks the write
  and is printed as a SKIP: record it in People first, so nothing is lost.
- **Photo** = the `person_photos` row `people.contact_photo_id` names. Nothing is
  uploaded for a person without one. The upload goes through port's
  `updateContactPhoto` call and its read-back.

What this command wrote is marked in the contact's own `clientData`
(`people-sync.company`, `people-sync.photo` = the picture's sha256), so a rerun writes
only what changed in People, re-uploads a picture removed in Google, and may replace a
company it wrote itself. Reads and writes go through gog's `gog api call` in People API
batches of 200 (`getBatchGet`, `batchUpdateContacts`). Dry run by default.
"""

import argparse
import hashlib
import json

from people_sync import photos, port, somadata
from people_sync.google_cleanup import company

PEOPLE_SQL = """
SELECT p.id, p.name, p.circles, p.contact_photo_id, ph.sha256 AS photo_sha,
  ph.r2_key AS photo_key, g.source_id AS google_id
FROM people p
JOIN person_accounts g ON g.person_id = p.id AND g.platform = 'google_contacts'
  AND g.deleted_at IS NULL AND g.source_id IS NOT NULL
LEFT JOIN person_photos ph ON ph.id = p.contact_photo_id AND ph.person_id = p.id
  AND ph.deleted_at IS NULL
WHERE p.deleted_at IS NULL
ORDER BY p.name, p.id, g.source_id
"""
EMPLOYMENTS_SQL = (
    "SELECT person_id, company, title FROM person_employments WHERE deleted_at IS NULL"
)
COMPANY_KEY = "people-sync.company"
PHOTO_KEY = "people-sync.photo"
BATCH = 200
API = ["api", "call", "people", "v1"]


def _norm(value: str | None) -> str:
    return (value or "").strip().casefold()


def _client(contact: dict) -> dict[str, str]:
    return {e.get("key"): e.get("value") for e in contact.get("clientData") or []}


def _orgs(contact: dict) -> list[tuple[str, str]]:
    return [
        (o.get("name") or "", o.get("title") or "")
        for o in contact.get("organizations") or []
        if o.get("name") or o.get("title")
    ]


def company_gaps(want, contact, names: set[str], titles: set[str]) -> list[str]:
    """Google values the write would lose: neither in People nor written by this command."""
    ours = _client(contact).get(COMPANY_KEY)
    gaps = []
    for name, title in _orgs(contact):
        held = name in (want, ours) or all(_norm(p) in names for p in name.split(", "))
        if name and not held:
            gaps.append(f"Google company {name!r} is not in People")
        if title and _norm(title) not in titles:
            gaps.append(f"Google title {title!r} is not in People")
    return gaps


def read(resources: list[str]) -> dict[str, dict | int]:
    """{resource: person, or the HTTP status it failed with}."""
    out: dict[str, dict | int] = {}
    for i in range(0, len(resources), BATCH):
        params = {
            "resourceNames": resources[i : i + BATCH],
            "personFields": "organizations,photos,clientData",
        }
        got = json.loads(port._gog([*API, "people.getBatchGet", "--params", json.dumps(params)]))
        for r in got.get("responses") or []:
            ok = r.get("httpStatusCode", 200) == 200 and r.get("person")
            out[r["requestedResourceName"]] = r["person"] if ok else r.get("httpStatusCode", 0)
    return out


def _upload(resource: str, row: dict) -> bool:
    try:
        image = photos.get_object(row["photo_key"])
        if hashlib.sha256(image).hexdigest() != row["photo_sha"]:
            raise port.PhotoError("the stored photo does not match its hash")
        port.upload_photo(resource, image)
    except (port.PhotoError, port.GogError) as exc:
        print(f"  PHOTO FAILED {resource}: {exc}")
        return False
    print(f"  PHOTO {resource}: uploaded and read back")
    return True


def _update(writes: list[dict], contacts: dict[str, dict]) -> None:
    """One batchUpdateContacts per mask and batch; each result is checked field by field."""
    for mask in ("organizations,clientData", "clientData"):
        group = [w for w in writes if ("company" in w) == mask.startswith("organizations")]
        for i in range(0, len(group), BATCH):
            body = {"contacts": {}, "updateMask": mask, "readMask": "organizations,clientData"}
            for w in group[i : i + BATCH]:
                contact = contacts[w["resource"]]
                client = {k: v for k, v in _client(contact).items() if k not in w["client"]}
                client.update({k: v for k, v in w["client"].items() if v is not None})
                person = {
                    "etag": contact["etag"],
                    "clientData": [{"key": k, "value": v} for k, v in client.items()],
                }
                if "company" in w:
                    person["organizations"] = [{"name": w["company"]}] if w["company"] else []
                body["contacts"][w["resource"]] = person
            args = [*API, "people.batchUpdateContacts", "--body", "@/dev/stdin"]
            got = json.loads(port._gog([*args, "--allow-write", "--force"], json.dumps(body)))
            results = got.get("updateResult") or {}
            for w in group[i : i + BATCH]:
                person = (results.get(w["resource"]) or {}).get("person") or {}
                problems = []
                if "company" in w and [n for n, _ in _orgs(person)] != (
                    [w["company"]] if w["company"] else []
                ):
                    problems.append("company")
                client = _client(person)
                if any(client.get(k) != v for k, v in w["client"].items()):
                    problems.append("marker")
                if problems:
                    print(f"  VERIFY FAILED {w['resource']}: {', '.join(problems)} not as written")
                else:
                    print(f"  WROTE {w['resource']}")


def run(people: list[str], apply: bool) -> None:
    rows = somadata.sql(PEOPLE_SQL)
    if people:
        rows = [r for r in rows if r["id"] in people]
    names: dict[str, set[str]] = {}
    titles: dict[str, set[str]] = {}
    for e in somadata.sql(EMPLOYMENTS_SQL):
        names.setdefault(e["person_id"], set()).add(_norm(e.get("company")))
        titles.setdefault(e["person_id"], set()).add(_norm(e.get("title")))
    contacts = read(list(dict.fromkeys(r["google_id"] for r in rows)))
    writes, uploads = [], []
    synced = skipped = 0
    for row in rows:
        resource, who = row["google_id"], f"{row['name']} ({row['id']})"
        contact = contacts.get(resource)
        if not isinstance(contact, dict):
            skipped += 1
            print(f"SKIP {who} -> {resource}: Google did not return it ({contact})")
            continue
        want = company(row["circles"])
        circles = {_norm(c) for c in json.loads(row["circles"] or "[]")}
        change: dict = {"resource": resource, "client": {}}
        lines = []
        have = [n for n, _ in _orgs(contact)]
        if have != ([want] if want else []) or any(t for _, t in _orgs(contact)):
            gaps = company_gaps(
                want, contact, circles | names.get(row["id"], set()), titles.get(row["id"], set())
            )
            if gaps:  # the photo can still go; the company waits for People
                skipped += 1
                print(f"SKIP {who} -> {resource}: {'; '.join(gaps)}")
            else:
                change["company"] = want
                change["client"][COMPANY_KEY] = want
                shown = "; ".join(
                    f"{n!r}" + (f" (title {t!r})" if t else "") for n, t in _orgs(contact)
                )
                lines.append(f"  company: {shown or 'none'} -> {repr(want) if want else 'none'}")
        if row["contact_photo_id"] and not row["photo_sha"]:
            lines.append("  photo: skipped (contact_photo_id names no live photo of theirs)")
        elif row["photo_sha"] and not (
            port.own_photo(contact) and _client(contact).get(PHOTO_KEY) == row["photo_sha"]
        ):
            lines.append("  photo: yes (will upload)")
            uploads.append((change, row))
        if not lines:
            synced += 1
            continue
        print(f"PUSH {who} -> {resource}")
        print("\n".join(lines))
        if "company" in change or (uploads and uploads[-1][0] is change):
            writes.append(change)
    if apply:
        done = [c["resource"] for c, row in uploads if _upload(c["resource"], row)]
        for change, row in uploads:
            if change["resource"] in done:
                change["client"][PHOTO_KEY] = row["photo_sha"]
        writes = [w for w in writes if w["client"]]
        if done:
            contacts.update(read(done))  # an upload changes the etag
        _update(writes, contacts)
    noun = "contact" if len(writes) == 1 else "contacts"
    verb = "updated" if apply else "to update"
    print(f"\n{len(writes)} {noun} {verb}, {skipped} skipped, {synced} in sync")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="people-sync push",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("target", choices=("google",), help="the address book to write")
    parser.add_argument(
        "--person", action="append", default=[], help="limit to a people.id (repeatable)"
    )
    parser.add_argument("--apply", action="store_true", help="write (default: preview)")
    args = parser.parse_args(argv)
    run(args.person, args.apply)
