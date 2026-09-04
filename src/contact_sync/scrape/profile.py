"""Platform-agnostic `Profile` shape and its upsert into `contact_profiles`.

One row per ledger record (`id = record_id`), latest scrape wins. JSON-shaped
columns (education, work, links) are serialized to text; everything else maps
straight across.
"""

import json
from dataclasses import dataclass, field

from contact_sync import lifedata

_JSON_COLUMNS = ("education", "work", "links")
_BOOL_COLUMNS = ("is_private", "is_verified")


@dataclass
class Profile:
    record_id: str = ""
    platform: str = ""
    profile_url: str = ""
    platform_id: str | None = None
    display_name: str | None = None
    bio: str | None = None
    location: str | None = None
    hometown: str | None = None
    education: list | None = None
    work: list | None = None
    birthday: str | None = None
    links: list | None = None
    is_private: bool | None = None
    is_verified: bool | None = None
    follower_count: int | None = None
    following_count: int | None = None
    mutual_count: int | None = None
    avatar_url: str | None = None
    raw: dict = field(default_factory=dict)


def _sql_value(value) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, int):
        return str(value)
    return lifedata.sq(str(value))


def _row(p: Profile, avatar_r2_key, avatar_sha256, raw_r2_key, scraped_at) -> dict:
    row = {
        "platform": p.platform,
        "profile_url": p.profile_url,
        "platform_id": p.platform_id,
        "display_name": p.display_name,
        "bio": p.bio,
        "location": p.location,
        "hometown": p.hometown,
        "education": p.education,
        "work": p.work,
        "birthday": p.birthday,
        "links": p.links,
        "is_private": p.is_private,
        "is_verified": p.is_verified,
        "follower_count": p.follower_count,
        "following_count": p.following_count,
        "mutual_count": p.mutual_count,
        "avatar_r2_key": avatar_r2_key,
        "avatar_sha256": avatar_sha256,
        "raw_r2_key": raw_r2_key,
        "scraped_at": scraped_at,
    }
    for col in _JSON_COLUMNS:
        if row[col] is not None:
            row[col] = json.dumps(row[col])
    for col in _BOOL_COLUMNS:
        if row[col] is not None:
            row[col] = int(row[col])
    return row


def upsert_profile(
    p: Profile,
    avatar_r2_key: str | None = None,
    avatar_sha256: str | None = None,
    raw_r2_key: str | None = None,
) -> None:
    row = _row(p, avatar_r2_key, avatar_sha256, raw_r2_key, lifedata.now_iso())
    existing = lifedata.sql(
        f"SELECT id FROM contact_profiles WHERE record_id = {lifedata.sq(p.record_id)}"
    )
    if existing:
        set_clause = ", ".join(f"{col} = {_sql_value(val)}" for col, val in row.items())
        lifedata.sql(
            f"UPDATE contact_profiles SET {set_clause} WHERE record_id = {lifedata.sq(p.record_id)}"
        )
    else:
        lifedata.insert("contact_profiles", [{"id": p.record_id, "record_id": p.record_id, **row}])
