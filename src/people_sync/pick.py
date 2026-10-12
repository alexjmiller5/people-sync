"""`people-sync photos pick`: give every person linked to a Google contact and without a
contact picture (`people.contact_photo_id`) one, from the first source in an order.

Sources: `contact` = the photo already set on their Google contact, `google_profile` =
the picture of their own Google profile (both fetched at full size and stored as
`google_contacts` photos first), any platform name = their newest stored photo from it.
A person who already has a contact picture is never touched. `push google` then sends
the picks to Google; a picture Google already shows is only marked there.

Dry run by default; `--apply` stores the Google photos it needs and writes the picks.
"""

import argparse
import hashlib
import sys
from collections import Counter

from people_sync import photos, push, somadata

DEFAULT_ORDER = (
    "contact",
    "instagram",
    "google_profile",
    "linkedin",
    "facebook",
    "venmo",
    "whatsapp",
    "partiful",
    "github",
    "spotify",
    "strava",
    "snapchat",
    "tiktok",
    "apple_contacts",
    "google_contacts",
)
GOOGLE = {"contact": "CONTACT", "google_profile": "PROFILE"}
CANDIDATES_SQL = """
SELECT p.id, p.name, g.source_id AS google_id
FROM people p
JOIN person_accounts g ON g.person_id = p.id AND g.platform = 'google_contacts'
  AND g.deleted_at IS NULL AND g.source_id IS NOT NULL
WHERE p.deleted_at IS NULL AND p.contact_photo_id IS NULL
ORDER BY p.name, p.id, g.source_id
"""
STORED_SQL = (
    "SELECT id, person_id, platform, fetched_at FROM person_photos WHERE deleted_at IS NULL"
)
BATCH = 200


def _choose(order, stored: list[dict], contacts: list[dict]):
    """The first source in `order` that has a picture: (source, photo id or Google URL)."""
    for source in order:
        if source in GOOGLE:
            url = next((u for c in contacts if (u := push.google_photo(c, GOOGLE[source]))), None)
            if url:
                yield source, None, url
        else:
            row = max(
                (r for r in stored if r["platform"] == source),
                key=lambda r: r["fetched_at"] or "",
                default=None,
            )
            if row:
                yield source, row["id"], None


def _store(person_id: str, url: str) -> str | None:
    image = photos.fetch_url_photo(url)
    if not image:
        return None
    ext = "png" if image.startswith(b"\x89PNG") else "jpg"
    photos.store_photo(person_id, "google_contacts", image, ext)
    sha = hashlib.sha256(image).hexdigest()
    found = somadata.sql(
        "SELECT id FROM person_photos WHERE deleted_at IS NULL "
        f"AND person_id = {somadata.sq(person_id)} AND sha256 = {somadata.sq(sha)}"
    )
    return found[0]["id"] if found else None


def run(order: tuple[str, ...], people: list[str], apply: bool) -> None:
    rows = somadata.sql(CANDIDATES_SQL)
    if people:
        rows = [r for r in rows if r["id"] in people]
    by_person: dict[str, list[dict]] = {}
    for row in rows:
        by_person.setdefault(row["id"], []).append(row)
    stored: dict[str, list[dict]] = {}
    for r in somadata.sql(STORED_SQL):
        stored.setdefault(r["person_id"], []).append(r)
    contacts = push.read(list(dict.fromkeys(r["google_id"] for r in rows)))
    picks: dict[str, str] = {}
    sources: Counter = Counter()
    none = 0
    for pid, person_rows in by_person.items():
        found = [c for r in person_rows if isinstance(c := contacts.get(r["google_id"]), dict)]
        for source, photo_id, url in _choose(order, stored.get(pid, []), found):
            if url and apply:
                photo_id = _store(pid, url)
                if not photo_id:
                    continue  # unreadable: the next source
            print(f"PICK {person_rows[0]['name']} ({pid}): {source}")
            picks[pid], sources[source] = photo_id, sources[source] + 1
            break
        else:
            none += 1
    if apply:
        items = list(picks.items())
        for i in range(0, len(items), BATCH):
            chunk = items[i : i + BATCH]
            cases = " ".join(f"WHEN {somadata.sq(p)} THEN {somadata.sq(ph)}" for p, ph in chunk)
            ids = ", ".join(somadata.sq(p) for p, _ in chunk)
            somadata.sql(
                f"UPDATE people SET contact_photo_id = CASE id {cases} END "
                f"WHERE id IN ({ids}) AND contact_photo_id IS NULL"
            )
    by_source = ", ".join(f"{s} {n}" for s, n in sources.most_common())
    verb = "picked" if apply else "to pick"
    print(f"\n{len(picks)} {verb}, {none} without a photo" + (f" ({by_source})" if picks else ""))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        prog="people-sync photos pick",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--order",
        default=",".join(DEFAULT_ORDER),
        help="comma-separated sources, first wins (default: %(default)s)",
    )
    parser.add_argument(
        "--person", action="append", default=[], help="limit to a people.id (repeatable)"
    )
    parser.add_argument("--apply", action="store_true", help="store and write (default: preview)")
    args = parser.parse_args(argv)
    order = tuple(s.strip() for s in args.order.split(",") if s.strip())
    unknown = [s for s in order if s not in DEFAULT_ORDER]
    if unknown:
        sys.exit(f"unknown source(s): {', '.join(unknown)}; known: {', '.join(DEFAULT_ORDER)}")
    run(order, args.person, args.apply)
