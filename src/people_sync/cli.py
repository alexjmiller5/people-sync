"""CLI entrypoint: uv run python -m people_sync <cmd>."""

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
from pathlib import Path

from people_sync import ledger, lifedata, match, notion_people, photos, sources
from people_sync import captures, google_cleanup, propose, reconcile, replay, review, whatsapp
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


def cmd_capture(args: argparse.Namespace) -> None:
    try:
        capture = captures.capture_export(args.source, args.path)
        key = captures.retain(capture, state_dir=args.state_dir)
    except Exception:
        sys.exit("capture failed; input or retention could not be verified")
    print(json.dumps({"key": key, "capture_id": capture["capture_id"]}))


def cmd_captures(args: argparse.Namespace) -> None:
    entries = []
    directory = captures.state_directory(args.state_dir) / "captures"
    for path in sorted(directory.glob("*.json")):
        try:
            c = json.loads(path.read_bytes())
            captures.validate(c)
            entries.append(
                {
                    key: c[key]
                    for key in (
                        "capture_id",
                        "source",
                        "kind",
                        "record_id",
                        "captured_at",
                        "payload_sha256",
                    )
                }
                | {"verification": "payload-checksum"}
            )
        except Exception:
            entries.append(
                {"verification": "invalid"}
                | ({"file_id": path.stem} if re.fullmatch(r"[0-9a-f]{32}", path.stem) else {})
            )
    print(json.dumps({"captures": entries}))


def cmd_replay(args: argparse.Namespace) -> None:
    try:
        original = Path(args.input).read_bytes()
        c = json.loads(original)
        # Old archive objects carried no source metadata. Recover only an explicit
        # source namespace from their retained path, never guess from profile fields.
        if isinstance(c, dict) and "schema_version" not in c and not c.get("source"):
            parts = Path(args.input).parts
            for i, part in enumerate(parts[:-1]):
                if part == "profiles" and parts[i + 1] in replay.PROFILE_SOURCES:
                    c = {**c, "source": parts[i + 1]}
                    break
        result = replay.replay_capture(c)
        if isinstance(c, dict) and "schema_version" not in c:
            result["input_sha256"] = hashlib.sha256(original).hexdigest()
        if args.compare:
            before = json.loads(Path(args.compare).read_bytes())
            a, b = replay.normalized(before), replay.normalized(result)
            result["comparison"] = {
                "changed": a != b,
                "diff": "\n".join(
                    difflib.unified_diff(
                        json.dumps(a, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
                        json.dumps(b, ensure_ascii=False, sort_keys=True, indent=2).splitlines(),
                        fromfile="previous",
                        tofile="current",
                        lineterm="",
                    )
                ),
            }
        if args.output:
            captures.write_private(args.output, captures.encode(result))
    except Exception:
        sys.exit("replay failed; local input or output is invalid")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    if result["status"] != "ok":
        sys.exit(1)


def cmd_ingest(args: argparse.Namespace) -> None:
    try:
        if args.source == "whatsapp":
            report = whatsapp.ingest(
                args.snapshot,
                args.media_dir,
                self_id=args.self_id,
                state_dir=args.state_dir,
                contacts=None if args.no_contacts else whatsapp.contact_lookup(),
            )
            print(json.dumps(report))
            return
        if args.source in captures.EXPORT_SOURCES:
            records = sources.retained_records(captures.capture_export(args.source, args.path))
        else:
            records = getattr(sources, f"fetch_{args.source}")()
    except Exception:
        sys.exit("ingest failed; input, retention or replay could not be verified")
    print(json.dumps(ledger.upsert(records)))


def cmd_match(args: argparse.Namespace) -> None:
    print(json.dumps(match.run_match()))


def cmd_queue(args: argparse.Namespace) -> None:
    print(json.dumps(lifedata.sql(QUEUE_QUERY)))


def cmd_new_person(args: argparse.Namespace) -> None:
    try:
        person_id, page_id = notion_people.new_person_id(args.name)
    except RuntimeError as e:
        sys.exit(str(e))
    try:
        lifedata.insert("people", [{"id": person_id, "name": args.name}])
    except Exception:
        if page_id:
            print(
                f"orphaned notion page {page_id}: created but life-data insert failed; "
                "re-run with this id or delete the page",
                file=sys.stderr,
            )
        raise
    print(person_id)


def _require_file_token() -> None:
    """Validate file service configuration before opening the source page."""
    for name in ("LIFE_HUB_TOKEN", "LIFE_HUB_URL"):
        if not os.environ.get(name):
            sys.exit(f"{name} is not set - profile pictures cannot be stored")


def cmd_scrape(args: argparse.Namespace) -> None:
    _require_file_token()
    result = scrape_run.scrape(
        args.platform,
        max_n=args.max,
        state_path=args.state,
        endpoint=args.endpoint,
        data_dir=args.data_dir,
        approve_command=args.approve_command,
        **({"targets": args.target} if args.target else {}),
        **({"record_id": args.record_id} if args.record_id is not None else {}),
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

    _require_file_token()
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
            # Recheck the same unique-name proposal for already assigned handles
            # so a failed evidence write can be repaired without changing a handle.
            proposals = facebook.assign_handles(entries, [{**r, "handle": None} for r in records])
            by_id = {r["id"]: r for r in records}
            updates = []
            for u in proposals:
                prior = by_id[u["id"]].get("handle")
                if prior and prior != u["handle"]:
                    continue
                if not prior:
                    lifedata.sql(
                        f"UPDATE people_sync_records SET handle = {lifedata.sq(u['handle'])} "
                        f"WHERE id = {lifedata.sq(u['id'])} AND deleted_at IS NULL"
                    )
                    updates.append(u)
                ledger.imported_from(
                    "people_sync_records", u["id"], u.get("capture_key"), u.get("capture_refs", ())
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
        elif args.platform == "spotify":
            from people_sync.scrape import spotify

            entries = spotify.list_users(browser)
            print(json.dumps({"entries": len(entries), **spotify.ingest_entries(entries)}))
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


def cmd_promote(args: argparse.Namespace) -> None:
    from people_sync import promote

    print(
        json.dumps(
            promote.run(apply_writes=args.apply, platforms=args.platform or promote.PLATFORMS)
        )
    )


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

    capture_p = sub.add_parser("capture", help="retain a privacy-filtered export without ingesting")
    capture_p.add_argument("source", choices=captures.EXPORT_SOURCES)
    capture_p.add_argument("--path", required=True, help="file, or directory for Instagram")
    capture_p.add_argument("--state-dir", help="private local state root (default: XDG state)")
    capture_p.set_defaults(func=cmd_capture)

    captures_p = sub.add_parser(
        "captures", help="inventory local captures and verify their checksums"
    )
    captures_p.add_argument("--state-dir")
    captures_p.add_argument(
        "--verify", action="store_true", help="explicit verification (always on)"
    )
    captures_p.set_defaults(func=cmd_captures)

    replay_p = sub.add_parser("replay", help="parse retained input offline into a proposal")
    replay_p.add_argument("--input", required=True)
    replay_p.add_argument("--compare", help="previous replay proposal JSON")
    replay_p.add_argument("--output", help="private proposal file outside source control")
    replay_p.set_defaults(func=cmd_replay)

    ingest = sub.add_parser("ingest", help="parse an export or fetch a source into the ledger")
    ingest_sub = ingest.add_subparsers(dest="source", required=True)
    for name in ("instagram", *_PARSE_WITH_PATH):
        p = ingest_sub.add_parser(name)
        p.add_argument("--path", required=True, help="export file, or export dir for instagram")
        p.set_defaults(func=cmd_ingest)
    for name in _FETCH_NO_PATH:
        p = ingest_sub.add_parser(name)
        p.set_defaults(func=cmd_ingest)
    wa = ingest_sub.add_parser("whatsapp", help="operator-supplied metadata snapshot, read-only")
    wa.add_argument("--snapshot", required=True, help="WAL-consistent metadata-only sqlite copy")
    wa.add_argument("--media-dir", required=True, help="root holding the cached profile pictures")
    wa.add_argument("--self-id", required=True, help="the account's own native JID (excluded)")
    wa.add_argument("--state-dir", help="private local state root (default: XDG state)")
    wa.add_argument(
        "--no-contacts",
        action="store_true",
        help="skip the local address-book lookup that cross-references each chat's number",
    )
    wa.set_defaults(func=cmd_ingest)

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
    scrape_p.add_argument(
        "--record-id", help="recapture exactly one pending/matched record, even if fresh"
    )
    scrape_p.add_argument("--state", default=DEFAULT_STATE_PATH)
    scrape_p.add_argument(
        "--target",
        action="append",
        help="caller-owned CDP tab ID; repeat up to ten times for a coordinated run",
    )
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
    list_p.add_argument("platform", choices=["facebook", "partiful", "strava", "spotify"])
    list_p.add_argument("--start", type=int, default=0, help="partiful: first row index")
    list_p.add_argument("--max", type=int, default=None, help="partiful: rows to process")
    _add_browser_options(list_p)
    list_p.set_defaults(func=cmd_list)

    promote_p = sub.add_parser(
        "promote",
        help="write scraped facts (city, job, birthday, photo) onto matched people, with provenance",
    )
    promote_p.add_argument(
        "--apply", action="store_true", help="write (default: print the plan only)"
    )
    promote_p.add_argument("--platform", action="append", help="limit to a platform (repeatable)")
    promote_p.set_defaults(func=cmd_promote)

    for name, (target, help_text) in DELEGATES.items():
        sub.add_parser(name, help=help_text, add_help=False)

    photos_p = sub.add_parser("photos", help="profile-photo storage")
    photos_sub = photos_p.add_subparsers(dest="photos_command", required=True)
    store = photos_sub.add_parser("store", help="store a scraped profile photo")
    store.add_argument("--person", required=True)
    store.add_argument("--platform", required=True)
    store.add_argument("--file", required=True)
    store.set_defaults(func=cmd_photos_store)

    return parser


# Operator tools with their own argparse surface: their argv is handed over
# whole (argparse's REMAINDER cannot carry a leading `--help`).
DELEGATES = {
    "reconcile": (reconcile.main, "triage moves: link / merge / create (dry run by default)"),
    "google-cleanup": (google_cleanup.cli, "clear Google labels/org fields already consolidated"),
    "review": (review.main, "render a private photo-assisted review page"),
    "propose": (propose.main, "propose identity clusters for the review page"),
}


def main(argv: list[str] | None = None) -> None:
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in DELEGATES:
        DELEGATES[argv[0]][0](argv[1:])
        return
    args = build_parser().parse_args(argv)
    args.func(args)
