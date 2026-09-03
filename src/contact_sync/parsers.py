"""Export parsers for instagram, facebook, snapchat, linkedin.

Real export shapes observed 2026-09 (structure only - no personal values):

Instagram followers.json - a flat JSON array, one entry per follower:
    [{"title": "", "media_list_data": [], "string_list_data": [
        {"href": "https://instagram.com/<user>", "value": "<user>", "timestamp": <int>}
    ]}, ...]
    string_list_data always holds exactly one item in the real export.

Instagram following.json - a JSON object wrapping the same entry shape:
    {"relationships_following": [ <same entry shape as followers.json> ]}

Facebook your_friends.json - a JSON object with a single top-level key:
    {"friends_v2": [{"name": "<Full Name>", "timestamp": <int>}, ...]}
    FB exports carry names only - no handle/username.

LinkedIn Connections.csv - a notes preamble ("Notes:" line + a quoted
paragraph + a blank line) before the real header row:
    First Name,Last Name,URL,Email Address,Company,Position,Connected On
Some rows are entirely blank except Connected On (LinkedIn could not export
that connection's profile) - these have no URL and are skipped.

Snapchat friends.json - UNVALIDATED against a real export (the file does not
exist as of 2026-09-03, only a placeholder). Written from Snapchat's
documented shape:
    {"friends": [{"Username": "<user>", "Display Name": "<name>", ...}]}
"""

import csv
import json

import structlog

from contact_sync.ledger import Record

log = structlog.get_logger(__name__)


def _ig_entries(path: str) -> dict[str, dict]:
    """lowercase username -> raw entry, from one Instagram export file."""
    with open(path) as f:
        data = json.load(f)
    entries = data if isinstance(data, list) else data.get("relationships_following", [])
    out: dict[str, dict] = {}
    for entry in entries:
        items = entry.get("string_list_data") or []
        username = items[0].get("value") if items else None
        if not username:
            log.warning("instagram entry missing username", entry=entry)
            continue
        out[username.lower()] = entry
    return out


def parse_instagram(followers_path: str, following_path: str) -> list[Record]:
    followers = _ig_entries(followers_path)
    following = _ig_entries(following_path)
    records = []
    for username in sorted(followers.keys() | following.keys()):
        f_entry = followers.get(username)
        g_entry = following.get(username)
        handle = (f_entry or g_entry)["string_list_data"][0]["value"]
        records.append(
            Record(
                source="instagram",
                source_id=username,
                handle=handle,
                name=None,
                raw={"followers": f_entry, "following": g_entry},
                follows_me=1 if f_entry else 0,
                i_follow=1 if g_entry else 0,
            )
        )
    return records


def parse_facebook(path: str) -> list[Record]:
    with open(path) as f:
        data = json.load(f)
    records = []
    for entry in data.get("friends_v2", []):
        name = entry.get("name")
        if not name:
            log.warning("facebook entry missing name", entry=entry)
            continue
        source_id = name.strip().lower().replace(" ", "_")
        records.append(
            Record(
                source="facebook",
                source_id=source_id,
                handle=None,
                name=name,
                raw=entry,
                follows_me=1,
                i_follow=1,
            )
        )
    return records


def parse_snapchat(path: str) -> list[Record]:
    with open(path) as f:
        data = json.load(f)
    records = []
    for entry in data.get("friends", []):
        username = entry.get("Username")
        if not username:
            log.warning("snapchat entry missing username", entry=entry)
            continue
        records.append(
            Record(
                source="snapchat",
                source_id=username.lower(),
                handle=username,
                name=entry.get("Display Name"),
                raw=entry,
                follows_me=None,
                i_follow=None,
            )
        )
    return records


def _linkedin_slug(url: str) -> str | None:
    if not url or "/in/" not in url:
        return None
    slug = url.split("/in/", 1)[1].split("?")[0].strip("/")
    return slug.lower() or None


def parse_linkedin(path: str) -> list[Record]:
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header_idx = next(i for i, row in enumerate(rows) if row[:1] == ["First Name"])
    header = rows[header_idx]
    records = []
    for row in rows[header_idx + 1 :]:
        if not row or not any(row):
            continue
        raw = dict(zip(header, row))
        slug = _linkedin_slug(raw.get("URL", ""))
        if not slug:
            log.warning("linkedin row missing profile url", raw=raw)
            continue
        name = f"{raw.get('First Name', '')} {raw.get('Last Name', '')}".strip() or None
        records.append(
            Record(
                source="linkedin",
                source_id=slug,
                handle=slug,
                name=name,
                raw=raw,
                follows_me=None,
                i_follow=None,
            )
        )
    return records
