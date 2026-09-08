"""CLI entrypoint: uv run python -m people_sync <cmd>."""

import argparse
import json
import os
import sys

from people_sync import ledger, lifedata, match, notion_people, parsers, photos, sources
from people_sync.scrape import cdp
from people_sync.scrape import login as scrape_login
from people_sync.scrape import run as scrape_run
from people_sync.scrape.pace import DEFAULT_STATE_PATH

QUEUE_QUERY = """
    SELECT c.id, c.source, c.handle, c.name,
           c.suggested_person_id, p.name AS suggested_name
    FROM people_sync_records c
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
    result = scrape_run.scrape(
        args.platform,
        max_n=args.max,
        state_path=args.state,
        endpoint=args.endpoint,
        data_dir=args.data_dir,
        approve_command=args.approve_command,
    )
    print(json.dumps(result))


def cmd_login(args: argparse.Namespace) -> None:
    try:
        result = scrape_login.login(
            args.platform,
            endpoint=args.endpoint,
            data_dir=args.data_dir,
            approve_command=args.approve_command,
        )
    except scrape_login.LoginError as e:
        sys.exit(str(e))
    print(json.dumps(result))
    if result["status"] == "halted":
        sys.exit(1)


def cmd_list(args: argparse.Namespace) -> None:
    """Capture a platform's friend/connection list from the logged-in browser:
    facebook gives the ledger's name-only records their handles; partiful
    clicks through every mutual and writes ledger + profile rows."""
    from people_sync.scrape.cdp import Browser

    browser = Browser.connect(
        endpoint=args.endpoint, data_dir=args.data_dir, approve_command=args.approve_command
    )
    try:
        if args.platform == "facebook":
            from people_sync.scrape import facebook

            entries = facebook.list_friends(browser)
            records = lifedata.sql(
                "SELECT id, name, handle FROM people_sync_records "
                "WHERE source = 'facebook' AND deleted_at IS NULL"
            )
            updates = facebook.assign_handles(entries, records)
            for u in updates:
                lifedata.sql(
                    f"UPDATE people_sync_records SET handle = {lifedata.sq(u['handle'])} "
                    f"WHERE id = {lifedata.sq(u['id'])}"
                )
            print(
                json.dumps(
                    {
                        "entries": len(entries),
                        "assigned": len(updates),
                        "unmatched_records": sum(1 for r in records if not r.get("handle"))
                        - len(updates),
                    }
                )
            )
        elif args.platform == "strava":
            from people_sync.scrape import strava

            entries = strava.list_athletes(browser)
            print(json.dumps({"entries": len(entries), **strava.ingest_entries(entries)}))
        else:
            from people_sync.scrape import partiful

            done = failed = 0
            for index, total, entry in partiful.harvest(browser, start=args.start, limit=args.max):
                if partiful.ingest_entry(entry, browser, index):
                    done += 1
                else:
                    failed += 1
                    scrape_run.log.warning(
                        "mutual failed", platform="partiful", index=index, reason=entry.get("error")
                    )
                if (index + 1) % 25 == 0:
                    scrape_run.log.info(
                        "harvest progress", platform="partiful", index=index, total=total
                    )
            print(json.dumps({"done": done, "failed": failed}))
    finally:
        browser.close()


def cmd_photos_store(args: argparse.Namespace) -> None:
    with open(args.file, "rb") as f:
        image = f.read()
    ext = os.path.splitext(args.file)[1].lstrip(".").lower()
    result = photos.store_photo(args.person, args.platform, image, ext)
    print(result if result else "duplicate")


def _add_browser_options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--endpoint",
        default=None,
        help="CDP host:port to drive instead of Chrome's default data dir",
    )
    parser.add_argument(
        "--data-dir", default=None, help="Chrome data dir to read DevToolsActivePort from"
    )
    parser.add_argument(
        "--approve-command",
        default=None,
        help=(
            "command that approves the browser's remote-debugging prompt on hosts that "
            f"show one (default: ${cdp.CDP_APPROVE_COMMAND_ENV}); ignored with --endpoint"
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="people_sync")
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
    _add_browser_options(scrape_p)
    scrape_p.set_defaults(func=cmd_scrape)

    login_p = sub.add_parser(
        "login", help="sign this platform's Chrome profile in (idempotent, halts on anything odd)"
    )
    login_p.add_argument("platform")
    _add_browser_options(login_p)
    login_p.set_defaults(func=cmd_login)

    list_p = sub.add_parser(
        "list", help="capture a platform's friends list and give name-only records their handles"
    )
    list_p.add_argument("platform", choices=["facebook", "partiful", "strava"])
    list_p.add_argument("--start", type=int, default=0, help="partiful: first row index")
    list_p.add_argument("--max", type=int, default=None, help="partiful: rows to process")
    _add_browser_options(list_p)
    list_p.set_defaults(func=cmd_list)

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
