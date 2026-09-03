"""One-off circles migration. Modes: worksheet | apply | sideeffects | reconcile.

PRESERVATION INVARIANT: the default for every value is a verbatim circle.
Any other action (merge/rename/split/location/employment/met_through/retire)
exists in the decisions file ONLY because Alex chose it for that value.
"""

import json
import pathlib
import sys
from collections import defaultdict

from contact_sync import lifedata

WS = pathlib.Path("data/circles_worksheet.json")
DEC = pathlib.Path("data/circles_decisions.json")


def load_people():
    return lifedata.sql(
        "SELECT id, tags, company, when_we_met, employer, position_title "
        "FROM people WHERE deleted_at IS NULL"
    )


def values_of(row):
    vals = []
    for t in json.loads(row.get("tags") or "[]"):
        vals.append(("tags", t))
    for col in ("company", "when_we_met"):
        v = (row.get(col) or "").strip()
        if v:
            vals.append((col, v))
    return vals


def worksheet():
    counts = defaultdict(lambda: {"count": 0, "sources": set()})
    for row in load_people():
        for src, v in values_of(row):
            counts[v]["count"] += 1
            counts[v]["sources"].add(src)
    WS.write_text(
        json.dumps(
            {
                v: {
                    "count": d["count"],
                    "sources": sorted(d["sources"]),
                    "decision": {"action": "circle", "circle": v},
                }
                for v, d in sorted(counts.items(), key=lambda kv: -kv[1]["count"])
            },
            indent=1,
        )
    )
    print(f"{len(counts)} distinct values -> {WS}")


def apply():
    dec = json.loads(DEC.read_text())
    people = load_people()
    unaccounted = set()
    for row in people:
        circles, changed = [], False
        for _, v in values_of(row):
            d = dec.get(v)
            if d is None:
                unaccounted.add(v)
                continue
            for c in circles_of(d):
                if c not in circles:
                    circles.append(c)
            changed = True
        # employments from the employer multi-select + position_title
        emps = json.loads(row.get("employer") or "[]")
        title = (row.get("position_title") or "").strip() or None
        emp_rows = [
            {
                "id": f"emp:{row['id']}:{i}",
                "person_id": row["id"],
                "company": e,
                "title": title if i == 0 else None,
                "start": None,
                "end": None,
                "source": "notion_import",
                "notes": None,
            }
            for i, e in enumerate(emps)
        ]
        lifedata.insert("person_employments", emp_rows)
        if changed or circles:
            lifedata.sql(
                f"UPDATE people SET circles = {lifedata.sq(json.dumps(circles))} "
                f"WHERE id = '{row['id']}'"
            )
    assert not unaccounted, (
        f"values with NO decision (nothing may be dropped): {sorted(unaccounted)}"
    )
    print("applied; run reconcile checks next")


def circles_of(decision):
    a = decision["decision"]
    return a.get("circles", [a.get("circle")] if a.get("circle") else [])


def sideeffects():
    """met_through edges for the values whose decision names a resolved person."""
    dec = json.loads(DEC.read_text())
    rows = {}
    for row in load_people():
        for _, v in values_of(row):
            mt = dec[v]["decision"].get("met_through")
            if mt and mt["person_id"] != row["id"]:
                rid = f"mt:{row['id']}:{mt['person_id']}"
                rows[rid] = {
                    "id": rid,
                    "person_id": row["id"],
                    "related_id": mt["person_id"],
                    "relation_type": "met_through",
                }
    lifedata.insert("person_relations", list(rows.values()))
    print(f"{len(rows)} met_through edges inserted")


def reconcile():
    """Every person who contributed a value must carry the circle(s) it maps to."""
    dec = json.loads(DEC.read_text())
    want = defaultdict(lambda: defaultdict(set))  # value -> circle -> person ids
    for row in load_people():
        for _, v in values_of(row):
            for c in circles_of(dec[v]):
                want[v][c].add(row["id"])
    have = defaultdict(set)
    for r in lifedata.sql(
        "SELECT p.id AS id, j.value AS c FROM people p, json_each(p.circles) j "
        "WHERE p.deleted_at IS NULL"
    ):
        have[r["c"]].add(r["id"])
    shortfalls = 0
    print(f"{'value':<56} {'n':>4}  -> circle (carriers, missing)")
    for v, d in sorted(dec.items(), key=lambda kv: -kv[1]["count"]):
        parts = []
        for c in circles_of(d) or ["<retired>"]:
            missing = len(want[v][c] - have[c]) if c in want[v] else 0
            shortfalls += missing
            parts.append(f"{c!r} ({len(have[c])}, -{missing})")
        print(f"{v!r:<56} {d['count']:>4}  -> " + "; ".join(parts))
    print(f"\n{len(dec)} values checked, {shortfalls} shortfalls")
    assert not shortfalls, "PRESERVATION VIOLATION: people lost a value"


if __name__ == "__main__":
    {
        "worksheet": worksheet,
        "apply": apply,
        "sideeffects": sideeffects,
        "reconcile": reconcile,
    }[sys.argv[1]]()
