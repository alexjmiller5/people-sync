"""CLI entrypoint: uv run python -m contact_sync <cmd>."""

import argparse
import json
import os
import sys

from contact_sync import ledger, lifedata, match, notion_people, parsers, photos, sources
from contact_sync.scrape import run as scrape_run
from contact_sync.scrape.pace import DEFAULT_STATE_PATH

QUEUE_QUERY = """
    SELECT c.id, c.source, c.handle, c.name,
           c.suggested_person_id, p.name AS suggested_name
    FROM contact_records c
    LEFT JOIN people p ON p.id = c.suggested_person_id
    WHERE c.status = 'pending'
    ORDER BY c.suggested_person_id IS NULL, c.id
"""

# Looked up as module attributes at call time (not bound at import time) so
# tests can mock.patch the underlying parser/source functions.
_PARSE_WITH_PATH = ("facebook", "snapchat", "linkedin")
_FETCH_NO_PATH = ("google", "apple")


def cmd_ingest(args: argparse.Namespace) -> None:
    if args.source == "instagram":
        records = parsers.parse_instagram(
            os.path.join(args.path, "followers.json"), os.path.join(args.path, "following.json")
        )
    elif args.source in _PARSE_WITH_PATH:
        records = getattr(parsers, f"parse_{args.source}")(args.path)
    else:
        records = getattr(sources, f"fetch_{args.source}")()
    print(json.dumps(ledger.upsert(records)))


def cmd_match(args: argparse.Namespace) -> None:
    print(json.dumps(match.run_match()))


def cmd_queue(args: argparse.Namespace) -> None:
    print(json.dumps(lifedata.sql(QUEUE_QUERY)))


def cmd_new_person(args: argparse.Namespace) -> None:
    try:
        page_id = notion_people.create_stub(args.name)
    except RuntimeError as e:
        sys.exit(str(e))
    person_id = page_id.replace("-", "")
    try:
        lifedata.insert("people", [{"id": person_id, "name": args.name}])
    except Exception:
        print(
            f"orphaned notion page {page_id}: created but life-data insert failed; "
            "re-run with this id or delete the page",
            file=sys.stderr,
        )
        raise
    print(person_id)


def cmd_scrape(args: argparse.Namespace) -> None:
    result = scrape_run.scrape(args.platform, max_n=args.max, state_path=args.state)
    print(json.dumps(result))


def cmd_photos_store(args: argparse.Namespace) -> None:
    with open(args.file, "rb") as f:
        image = f.read()
    ext = os.path.splitext(args.file)[1].lstrip(".").lower()
    result = photos.store_photo(args.person, args.platform, image, ext)
    print(result if result else "duplicate")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="contact_sync")
    sub = parser.add_subparsers(dest="command", required=True)

    ingest = sub.add_parser("ingest", help="parse an export or fetch a source into the ledger")
    ingest_sub = ingest.add_subparsers(dest="source", required=True)
    for name in ("instagram", *_PARSE_WITH_PATH):
        p = ingest_sub.add_parser(name)
        p.add_argument("--path", required=True, help="export file, or export dir for instagram")
        p.set_defaults(func=cmd_ingest)
    for name in _FETCH_NO_PATH:
        p = ingest_sub.add_parser(name)
        p.set_defaults(func=cmd_ingest)

    match_p = sub.add_parser("match", help="auto-link pending ledger records to people")
    match_p.set_defaults(func=cmd_match)

    queue_p = sub.add_parser("queue", help="list pending ledger records for triage")
    queue_p.set_defaults(func=cmd_queue)

    new_person = sub.add_parser("new-person", help="create a Notion People stub + life-data row")
    new_person.add_argument("--name", required=True)
    new_person.set_defaults(func=cmd_new_person)

    scrape_p = sub.add_parser("scrape", help="scrape a platform's pending/matched profiles")
    scrape_p.add_argument("platform")
    scrape_p.add_argument("--max", type=int, default=None)
    scrape_p.add_argument("--state", default=DEFAULT_STATE_PATH)
    scrape_p.set_defaults(func=cmd_scrape)

    photos_p = sub.add_parser("photos", help="profile-photo storage")
    photos_sub = photos_p.add_subparsers(dest="photos_command", required=True)
    store = photos_sub.add_parser("store", help="store a scraped profile photo")
    store.add_argument("--person", required=True)
    store.add_argument("--platform", required=True)
    store.add_argument("--file", required=True)
    store.set_defaults(func=cmd_photos_store)

    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)
