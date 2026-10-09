"""`people-sync unfollow`: the accounts marked "not someone I know" (ledger status
`ignored`) that the user still follows, per platform with the profile URL - the
to-do list for unfollowing. A record drops off once a later export shows
`i_follow = 0`; platforms without a follow relation are never listed."""

import argparse
import json
import os
import sys

from people_sync import somadata

FOLLOW_PLATFORMS = ("instagram", "facebook", "linkedin", "snapchat", "venmo", "spotify", "strava")
# Export-only sources keep no profile URL in `raw`; their handle is the URL.
URL_FROM_HANDLE = {
    "instagram": "https://www.instagram.com/{handle}/",
    "linkedin": "https://www.linkedin.com/in/{handle}",
}


def pending() -> list[dict]:
    rows = somadata.sql(
        "SELECT id, source, name, handle, i_follow, json_extract(raw, '$.url') AS url, "
        "json_extract(raw, '$.people_sync_relationship.operation') AS removed_operation, "
        "json_extract(raw, '$.people_sync_relationship.state') AS relationship_state "
        "FROM people_sync_records WHERE status = 'ignored' AND deleted_at IS NULL "
        "ORDER BY source, name, handle"
    )
    out = []
    for r in rows:
        removed_operation = r.pop("removed_operation", None)
        relationship_state = r.pop("relationship_state", None)
        if relationship_state == "absent" and removed_operation == {
            "facebook": "unfriend",
            "linkedin": "remove-connection",
            "venmo": "remove-friend",
        }.get(r["source"]):
            continue
        if r["source"] not in FOLLOW_PLATFORMS or r.get("i_follow") == 0:
            continue
        if not r.get("url") and r.get("handle") and r["source"] in URL_FROM_HANDLE:
            r["url"] = URL_FROM_HANDLE[r["source"]].format(handle=r["handle"])
        out.append(r)
    return out


def main(argv: list[str] | None = None) -> None:
    from people_sync import unfollow_actions as actions

    parser = argparse.ArgumentParser(
        prog="people-sync unfollow", description=__doc__, allow_abbrev=False
    )
    parser.add_argument("--json", action="store_true")
    commands = parser.add_subparsers(dest="command")
    plan = commands.add_parser(
        "plan", help="freeze an exact batch; never changes a platform", allow_abbrev=False
    )
    plan.add_argument("--platform", required=True, choices=tuple(actions.OPERATIONS))
    plan.add_argument("--operation", required=True, choices=tuple(actions.OPERATIONS.values()))
    plan.add_argument("--actor", required=True, help="exact signed-in account handle")
    plan.add_argument(
        "--remote-id",
        action="append",
        default=[],
        help="observed RECORD_ID=NUMERIC_ID; repeat per target",
    )
    plan.add_argument("--record-id", action="append", required=True, dest="ids")
    prepare = commands.add_parser(
        "prepare",
        help="read live identities and relationships, then freeze a batch",
        allow_abbrev=False,
    )
    prepare.add_argument("--platform", required=True, choices=tuple(actions.OPERATIONS))
    prepare.add_argument(
        "--actor", required=True, help="exact signed-in account handle (numeric ID on Facebook)"
    )
    prepare.add_argument("--record-id", action="append", required=True, dest="ids")
    prepare.add_argument("--endpoint")
    prepare.add_argument("--target", help="caller-owned CDP tab ID")
    for name in ("apply", "resume"):
        p = commands.add_parser(
            name,
            allow_abbrev=False,
            help=(
                "preview, require typed TTY confirmation, then act"
                if name == "apply"
                else "verify attempted actions only; never repeat a removal"
            ),
        )
        p.add_argument("plan")
        p.add_argument("--endpoint")
        p.add_argument("--target", help="caller-owned CDP tab ID")
    commands.add_parser(
        "journal", allow_abbrev=False, help="read private action and recovery state as JSON"
    )
    args = parser.parse_args(argv)
    if args.command:
        if args.json:
            parser.error("--json is only available for the read-only report")
        try:
            if args.command == "journal":
                print(json.dumps(actions.read_journal(), indent=2))
            elif args.command == "prepare":
                batch, observations = actions.prepare_plan(
                    args.platform,
                    args.actor,
                    args.ids,
                    endpoint=args.endpoint,
                    target_id=args.target or os.environ.get("PEOPLE_SYNC_CDP_TARGET"),
                )
                path = actions.save_plan(batch)
                observed = actions.save_observations(batch, observations)
                print(actions.preview(batch))
                for item in observations:
                    print(f"  {item['record_id']}: {item['state']}")
                print(f"\nPrivate plan: {path}\nObserved relationships: {observed}")
            elif args.command == "plan":
                remote_ids = {}
                for item in args.remote_id:
                    key, sep, value = item.partition("=")
                    actions.require(
                        sep and key not in remote_ids, "invalid or duplicate --remote-id"
                    )
                    remote_ids[key] = value
                batch = actions.make_plan(
                    args.platform, args.operation, args.actor, args.ids, remote_ids=remote_ids
                )
                path = actions.save_plan(batch)
                print(actions.preview(batch))
                print(f"\nPrivate plan: {path}")
            else:
                batch = actions.load_plan(args.plan, expired_ok=args.command == "resume")
                result = actions.execute(
                    batch,
                    resume=args.command == "resume",
                    endpoint=args.endpoint,
                    target_id=args.target or os.environ.get("PEOPLE_SYNC_CDP_TARGET"),
                )
                print(json.dumps(result))
                if result["unattempted"]:
                    print("Partial batch: unattempted targets need a new plan and approval.")
                    raise SystemExit(2)
        except actions.Refused as e:
            print(f"Unfollow refused: {e}", file=sys.stderr)
            raise SystemExit(1) from None
        except KeyboardInterrupt:
            print(
                "Interrupted; resume only verifies uncertain actions, never retries them.",
                file=sys.stderr,
            )
            raise SystemExit(130) from None
        except Exception as e:
            print(
                f"Unfollow halted ({type(e).__name__}); inspect the journal and use resume. No automatic retry.",
                file=sys.stderr,
            )
            raise SystemExit(1) from None
        return
    rows = pending()
    if args.json:
        print(json.dumps(rows, indent=2))
        return
    platform = None
    for r in rows:
        if r["source"] != platform:
            platform = r["source"]
            print(f"\n{platform}")
        print(f"  {r.get('name') or r.get('handle') or r['id']}  {r.get('url') or r['id']}")
    print(f"\n{len(rows)} to unfollow")
