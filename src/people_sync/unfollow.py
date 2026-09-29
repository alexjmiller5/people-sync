"""`people-sync unfollow`: the accounts marked "not someone I know" (ledger status
`ignored`) that the user still follows, per platform with the profile URL - the
to-do list for unfollowing. A record drops off once a later export shows
`i_follow = 0`; platforms without a follow relation are never listed."""

import argparse
import json

from people_sync import lifedata

FOLLOW_PLATFORMS = ("instagram", "facebook", "linkedin", "snapchat", "venmo", "spotify", "strava")
# Export-only sources keep no profile URL in `raw`; their handle is the URL.
URL_FROM_HANDLE = {
    "instagram": "https://www.instagram.com/{handle}/",
    "linkedin": "https://www.linkedin.com/in/{handle}",
}


def pending() -> list[dict]:
    rows = lifedata.sql(
        "SELECT id, source, name, handle, i_follow, json_extract(raw, '$.url') AS url "
        "FROM people_sync_records WHERE status = 'ignored' AND deleted_at IS NULL "
        "ORDER BY source, name, handle"
    )
    out = []
    for r in rows:
        if r["source"] not in FOLLOW_PLATFORMS or r.get("i_follow") == 0:
            continue
        if not r.get("url") and r.get("handle") and r["source"] in URL_FROM_HANDLE:
            r["url"] = URL_FROM_HANDLE[r["source"]].format(handle=r["handle"])
        out.append(r)
    return out


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
