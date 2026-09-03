"""Google write-back cleanup: clear labels/org from Google once life-data holds them.

Google Contacts is the phone book (name, phones, emails, addresses, birthday,
photo - what the iPhone needs); life-data is the brain (circles, employments,
socials, notes). This script removes the semantic duplication from Google, one
contact at a time, and ONLY after re-reading life-data and confirming the
replacement is already there: every Google label must already be a circle and
every org name/title must already be a person_employments row. A contact with
any gap is printed and skipped - nothing is cleared from Google before its
life-data replacement is verified.

    uv run python scripts/google_cleanup.py             # dry run (the default)
    uv run python scripts/google_cleanup.py --apply     # with Alex watching
    uv run python scripts/google_cleanup.py --selftest  # offline assert check
"""

import argparse
import json
import sys

from contact_sync import lifedata, sources

RECORDS_QUERY = """
    SELECT c.source_id, c.raw, c.person_id, p.name, p.circles
    FROM contact_records c
    JOIN people p ON p.id = c.person_id
    WHERE c.deleted_at IS NULL AND p.deleted_at IS NULL
      AND c.source = 'google_contacts' AND c.status = 'matched'
    ORDER BY c.source_id
"""


def norm(s: str | None) -> str:
    return (s or "").strip().casefold()


def user_groups() -> dict[str, str]:
    """{contactGroups/<id>: label name} for the user's OWN labels (system ones are Google's)."""
    out = json.loads(
        sources._run(
            [
                "gog",
                "api",
                "call",
                "people",
                "v1",
                "contactGroups.list",
                "--params",
                '{"pageSize": 200}',
                "-j",
                "--no-input",
            ]
        )
    )
    return {
        g["resourceName"]: g.get("name", "")
        for g in out.get("contactGroups", [])
        if g.get("groupType") == "USER_CONTACT_GROUP"
    }


def decide(raw: dict, circles: list[str], employments: list[dict], groups: dict[str, str]):
    """Verify-then-clear decision for one contact.

    Returns (labels, clear_org, gaps): labels = [(group resourceName, name)] safe to
    drop, clear_org = whether the org/title fields are safe to clear, gaps = the
    life-data values that are missing. Any gap means nothing is cleared.
    """
    labels = []
    for membership in raw.get("labels") or []:
        rn = (membership.get("contactGroupMembership") or {}).get("contactGroupResourceName")
        if rn in groups:
            labels.append((rn, groups[rn]))

    orgs = [o for o in (raw.get("org") or []) if o.get("name") or o.get("title")]
    have_circles = {norm(c) for c in circles}
    have_companies = {norm(e.get("company")) for e in employments}
    have_titles = {norm(e.get("title")) for e in employments}

    gaps = [
        f"label {name!r} is not a circle" for _, name in labels if norm(name) not in have_circles
    ]
    for o in orgs:
        if o.get("name") and norm(o["name"]) not in have_companies:
            gaps.append(f"org {o['name']!r} is not an employment company")
        if o.get("title") and norm(o["title"]) not in have_titles:
            gaps.append(f"title {o['title']!r} is not an employment title")

    if gaps:
        return [], False, gaps
    return labels, bool(orgs), []


def clear(resource_name: str, labels: list[tuple[str, str]], clear_org: bool) -> None:
    for group_rn, _ in labels:
        sources._run(
            [
                "gog",
                "api",
                "call",
                "people",
                "v1",
                "contactGroups.members.modify",
                "--params",
                json.dumps({"resourceName": group_rn}),
                "--body",
                json.dumps({"resourceNamesToRemove": [resource_name]}),
                "--allow-write",
                "--force",
                "--no-input",
                "-j",
            ]
        )
    if clear_org:
        sources._run(
            [
                "gog",
                "contacts",
                "update",
                resource_name,
                "--org",
                "",
                "--title",
                "",
                "--no-input",
                "-y",
                "-j",
            ]
        )


def main(apply: bool) -> None:
    records = lifedata.sql(RECORDS_QUERY)
    employments = lifedata.sql(
        "SELECT person_id, company, title FROM person_employments WHERE deleted_at IS NULL"
    )
    by_person: dict[str, list[dict]] = {}
    for e in employments:
        by_person.setdefault(e["person_id"], []).append(e)
    groups = user_groups() if records else {}

    cleared = skipped = 0
    for rec in records:
        raw = json.loads(rec["raw"] or "{}")
        circles = json.loads(rec["circles"] or "[]")
        labels, clear_org, gaps = decide(raw, circles, by_person.get(rec["person_id"], []), groups)
        if gaps:
            skipped += 1
            print(f"SKIP {rec['name']}: " + "; ".join(gaps))
            continue
        if not labels and not clear_org:
            continue
        what = ", ".join([n for _, n in labels] + (["org"] if clear_org else []))
        if apply:
            clear(rec["source_id"], labels, clear_org)
            print(f"CLEARED {rec['name']}: {what}")
        else:
            print(f"WOULD CLEAR {rec['name']}: {what}")
        cleared += 1
    verb = "cleared" if apply else "would clear"
    print(f"\n{len(records)} matched google contacts, {cleared} {verb}, {skipped} skipped (gaps)")


def selftest() -> None:
    groups = {"contactGroups/1": "Capital One", "contactGroups/2": "NYC"}
    raw = {
        "labels": [
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/1"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/2"}},
            {"contactGroupMembership": {"contactGroupResourceName": "contactGroups/myContacts"}},
        ],
        "org": [{"name": "Acme", "title": "Engineer"}],
    }
    employments = [{"company": "Acme", "title": "Engineer"}]

    # Fully covered: both labels are circles, the org is an employment.
    labels, clear_org, gaps = decide(raw, ["Capital One", "NYC"], employments, groups)
    assert gaps == [], gaps
    assert clear_org is True
    assert [n for _, n in labels] == ["Capital One", "NYC"], labels  # system group ignored

    # A missing circle blocks the WHOLE contact, org included.
    labels, clear_org, gaps = decide(raw, ["Capital One"], employments, groups)
    assert labels == [] and clear_org is False
    assert gaps == ["label 'NYC' is not a circle"], gaps

    # A missing employment blocks it too, labels included.
    labels, clear_org, gaps = decide(raw, ["Capital One", "NYC"], [], groups)
    assert labels == [] and clear_org is False
    assert len(gaps) == 2, gaps

    # Case and whitespace noise is not a gap.
    assert (
        decide(raw, [" capital one ", "nyc"], [{"company": "ACME", "title": "engineer"}], groups)[2]
        == []
    )

    # No labels and no org: nothing to do, and no gap either.
    assert decide({}, [], [], groups) == ([], False, [])
    print("selftest ok")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="actually write to Google")
    parser.add_argument("--selftest", action="store_true", help="offline decision check")
    args = parser.parse_args()
    if args.selftest:
        selftest()
        sys.exit(0)
    main(args.apply)
