"""One-off: move flat handle columns into person_accounts, keeping source values verbatim."""

from contact_sync import lifedata

COLS = {  # people column -> (platform, which field the value fills)
    "instagram": ("instagram", "url"),
    "facebook": ("facebook", "url"),
    "snapchat": ("snapchat", "handle"),
    "linkedin_url": ("linkedin", "url"),
    "google_contacts_url": ("google_contacts", "url"),
}

rows = lifedata.sql(
    "SELECT id, instagram, facebook, snapchat, linkedin_url, google_contacts_url "
    "FROM people WHERE deleted_at IS NULL"
)
out, expected = [], 0
for r in rows:
    for col, (platform, field) in COLS.items():
        val = (r.get(col) or "").strip()
        if not val:
            continue
        expected += 1
        acct = {
            "id": f"{platform}:{r['id']}",
            "person_id": r["id"],
            "platform": platform,
            "handle": None,
            "url": None,
            "source_id": None,
            "display_name": None,
            "active": 1,
            "notes": None,
        }
        acct[field] = val
        if field == "url" and platform == "instagram":
            acct["handle"] = val.rstrip("/").rsplit("/", 1)[-1] or None
        out.append(acct)
lifedata.insert("person_accounts", out)
got = lifedata.sql("SELECT count(*) AS n FROM person_accounts WHERE deleted_at IS NULL")[0]["n"]
print(f"expected {expected}, inserted total now {got}")
assert got == expected, "count mismatch - do NOT drop columns"
