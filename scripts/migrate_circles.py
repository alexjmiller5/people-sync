"""One-off circles migration. Modes: worksheet | apply.

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
            a = d["decision"]
            for c in a.get("circles", [a.get("circle")] if a.get("circle") else []):
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


if __name__ == "__main__":
    {"worksheet": worksheet, "apply": apply}[sys.argv[1]]()
