"""`people-sync unfollow`: the accounts marked "not someone I know" (ledger status
`ignored`) that the user still follows, per platform with the profile URL - the
to-do list for unfollowing. A record drops off once a later export shows
`i_follow = 0`; platforms without a follow relation are never listed."""

import argparse
import json

from people_sync import lifedata

FOLLOW_PLATFORMS = ("instagram", "facebook", "linkedin", "snapchat", "venmo", "spotify", "strava")


def pending() -> list[dict]:
    rows = lifedata.sql(
        "SELECT id, source, name, handle, i_follow, json_extract(raw, '$.url') AS url "
        "FROM people_sync_records WHERE status = 'ignored' AND deleted_at IS NULL "
        "ORDER BY source, name, handle"
    )
    return [r for r in rows if r["source"] in FOLLOW_PLATFORMS and r.get("i_follow") != 0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="people-sync unfollow", description=__doc__)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
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
