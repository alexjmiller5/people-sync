"""Promote scraped profile facts onto the person they were matched to.

A profile row is a cache; a fact becomes estate data only here, and only
with a provenance edge (life-map: no edge, no write). Conservative by
construction: fills empty values and adds rows, never overwrites or
closes anything - a conflict (a different birthday, a different current
city) is reported for triage, not resolved.

Facts and where they go:
- location  -> person_locations (open row, city verbatim, source=platform)
- work[0]   -> person_employments (open row, company, source=platform)
- birthday  -> people.birthday (YYYY-MM-DD or --MM-DD, when empty) or
               people.slightly_known_birthday (--MM, when empty)
- avatar    -> person_photos (the scrape's R2 object, sha-deduped per person)

Dry-run by default; `--apply` writes. Every write is idempotent through
the deterministic provenance edge id.
"""

import json
import os
import re
from dataclasses import dataclass, field

from people_sync import lifedata

FROM_KIND = "people_sync_profiles"
PLATFORMS = ("facebook", "linkedin", "strava", "instagram", "partiful", "spotify", "venmo")


@dataclass
class Op:
    kind: str  # location | employment | birthday | photo | conflict
    person_id: str
    record_id: str
    platform: str
    value: str
    detail: dict = field(default_factory=dict)


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _asserted_by() -> str:
    return os.environ.get("PEOPLE_SYNC_ASSERTED_BY") or "script:people-sync promote"


def plan(
    profiles: list[dict],
    people: dict[str, dict],
    locations: list[dict],
    employments: list[dict],
    photos: list[dict],
    edges: set[str],
) -> list[Op]:
    """Pure: what would be written, given the current estate."""
    open_locs: dict[str, set[str]] = {}
    for row in locations:
        if row.get("end") is None:
            open_locs.setdefault(row["person_id"], set()).add(_norm(row.get("city")))
    open_jobs: dict[str, set[str]] = {}
    for row in employments:
        if row.get("end") is None:
            open_jobs.setdefault(row["person_id"], set()).add(_norm(row.get("company")))
    photo_shas: dict[str, set[str]] = {}
    for row in photos:
        photo_shas.setdefault(row["person_id"], set()).add(row.get("sha256") or "")

    ops: list[Op] = []
    for p in profiles:
        pid, rid, plat = p["person_id"], p["record_id"], p["platform"]
        person = people.get(pid)
        if not person:
            continue

        def edge(to_kind: str, to_ref: str) -> str:
            return f"{FROM_KIND}:{rid}:{to_ref}"

        city = (p.get("location") or "").strip()
        if city:
            if _norm(city) in open_locs.get(pid, set()):
                pass
            elif open_locs.get(pid):
                ops.append(
                    Op(
                        "conflict",
                        pid,
                        rid,
                        plat,
                        city,
                        {"field": "city", "existing": sorted(open_locs[pid])},
                    )
                )
            elif edge("person_locations", f"loc:{pid}:{rid}") not in edges:
                ops.append(
                    Op("location", pid, rid, plat, city, {"cue": p.get("location_cue") or city})
                )

        work = p.get("work") or []
        company = (work[0] if isinstance(work, list) and work else "").strip() if work else ""
        if (
            company
            and _norm(company) not in open_jobs.get(pid, set())
            and edge("person_employments", f"emp:{pid}:{rid}") not in edges
        ):
            ops.append(Op("employment", pid, rid, plat, company, {"cue": company}))

        bday = p.get("birthday")
        if bday:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}|--\d{2}-\d{2}", bday):
                existing = person.get("birthday")
                if not existing:
                    if edge("people", pid) + ":birthday" not in edges:
                        ops.append(Op("birthday", pid, rid, plat, bday, {"cue": bday}))
                elif existing != bday and not (
                    existing.startswith("--") and bday.endswith(existing[2:])
                ):
                    ops.append(
                        Op(
                            "conflict",
                            pid,
                            rid,
                            plat,
                            bday,
                            {"field": "birthday", "existing": existing},
                        )
                    )
                elif existing.startswith("--") and len(bday) == 10 and bday.endswith(existing[2:]):
                    ops.append(
                        Op("birthday", pid, rid, plat, bday, {"cue": bday, "upgrades": existing})
                    )
            elif (
                re.fullmatch(r"--\d{2}", bday)
                and not person.get("slightly_known_birthday")
                and not person.get("birthday")
            ):
                ops.append(
                    Op(
                        "birthday_month",
                        pid,
                        rid,
                        plat,
                        bday,
                        {"cue": p.get("birthday_cue") or bday},
                    )
                )

        if (
            p.get("avatar_r2_key")
            and p.get("avatar_sha256")
            and p["avatar_sha256"] not in photo_shas.get(pid, set())
        ):
            ops.append(
                Op(
                    "photo",
                    pid,
                    rid,
                    plat,
                    p["avatar_r2_key"],
                    {"sha256": p["avatar_sha256"], "fetched_at": p.get("scraped_at")},
                )
            )
    return ops


def load_state(
    platforms=PLATFORMS,
) -> tuple[list[dict], dict[str, dict], list[dict], list[dict], list[dict], set[str]]:
    plats = ", ".join(lifedata.sq(p) for p in platforms)
    profiles = lifedata.sql(
        "SELECT p.record_id, p.platform, p.location, p.work, p.birthday, p.avatar_r2_key, p.avatar_sha256, p.scraped_at, "
        "r.person_id FROM people_sync_profiles p JOIN people_sync_records r ON r.id = p.record_id "
        f"WHERE p.deleted_at IS NULL AND r.deleted_at IS NULL AND r.status = 'matched' AND r.person_id IS NOT NULL AND p.platform IN ({plats})"
    )
    for p in profiles:
        if isinstance(p.get("work"), str):
            try:
                p["work"] = json.loads(p["work"])
            except json.JSONDecodeError:
                p["work"] = None
    people = {
        r["id"]: r
        for r in lifedata.sql(
            "SELECT id, name, birthday, slightly_known_birthday FROM people WHERE deleted_at IS NULL"
        )
    }
    locations = lifedata.sql(
        "SELECT person_id, city, end FROM person_locations WHERE deleted_at IS NULL"
    )
    employments = lifedata.sql(
        "SELECT person_id, company, end FROM person_employments WHERE deleted_at IS NULL"
    )
    photos = lifedata.sql("SELECT person_id, sha256 FROM person_photos WHERE deleted_at IS NULL")
    edges = {
        e["id"]
        for e in lifedata.sql(
            f"SELECT id FROM provenance WHERE from_kind = {lifedata.sq(FROM_KIND)} AND deleted_at IS NULL"
        )
    }
    return profiles, people, locations, employments, photos, edges


def _edge(op: Op, to_kind: str, to_ref: str, field_name: str | None, created: bool) -> dict:
    detail = {"cue": op.detail.get("cue", op.value), "confidence": "high"}
    if created:
        detail["created_row"] = 1
    return {
        "id": f"{FROM_KIND}:{op.record_id}:{to_ref}"
        + (f":{field_name}" if to_kind == "people" else ""),
        "from_kind": FROM_KIND,
        "from_ref": op.record_id,
        "to_kind": to_kind,
        "to_ref": to_ref,
        "field": field_name,
        "rel": "evidence_of",
        "detail": json.dumps(detail),
        "asserted_by": _asserted_by(),
    }


def apply(ops: list[Op]) -> dict:
    """Write the plan: rows + their edges, one op at a time (each op is
    independently idempotent, so a crash mid-way is a re-run)."""
    counts: dict[str, int] = {}
    edges: list[dict] = []
    for op in ops:
        if op.kind == "conflict":
            continue
        if op.kind == "location":
            row_id = f"loc:{op.person_id}:{op.record_id}"
            lifedata.insert(
                "person_locations",
                [
                    {
                        "id": row_id,
                        "person_id": op.person_id,
                        "city": op.value,
                        "country": None,
                        "start": None,
                        "end": None,
                        "source": op.platform,
                        "notes": None,
                    }
                ],
            )
            edges.append(_edge(op, "person_locations", row_id, "city", True))
        elif op.kind == "employment":
            row_id = f"emp:{op.person_id}:{op.record_id}"
            lifedata.insert(
                "person_employments",
                [
                    {
                        "id": row_id,
                        "person_id": op.person_id,
                        "company": op.value,
                        "title": None,
                        "start": None,
                        "end": None,
                        "source": op.platform,
                        "notes": None,
                    }
                ],
            )
            edges.append(_edge(op, "person_employments", row_id, "company", True))
        elif op.kind == "birthday":
            lifedata.sql(
                f"UPDATE people SET birthday = {lifedata.sq(op.value)} WHERE id = {lifedata.sq(op.person_id)}"
            )
            edges.append(_edge(op, "people", op.person_id, "birthday", False))
        elif op.kind == "birthday_month":
            lifedata.sql(
                f"UPDATE people SET slightly_known_birthday = {lifedata.sq(op.value)} WHERE id = {lifedata.sq(op.person_id)}"
            )
            edges.append(_edge(op, "people", op.person_id, "slightly_known_birthday", False))
        elif op.kind == "photo":
            row_id = f"photo:{op.person_id}:{op.record_id}"
            lifedata.insert(
                "person_photos",
                [
                    {
                        "id": row_id,
                        "person_id": op.person_id,
                        "platform": op.platform,
                        "r2_key": op.value,
                        "sha256": op.detail.get("sha256"),
                        "fetched_at": op.detail.get("fetched_at") or lifedata.now_iso(),
                        "notes": None,
                    }
                ],
            )
            edges.append(_edge(op, "person_photos", row_id, "r2_key", True))
        counts[op.kind] = counts.get(op.kind, 0) + 1
    if edges:
        lifedata.insert("provenance", edges)
    return counts


def run(apply_writes: bool = False, platforms=PLATFORMS) -> dict:
    ops = plan(*load_state(platforms))
    summary = {"planned": {}, "conflicts": [], "applied": {}}
    for op in ops:
        if op.kind == "conflict":
            summary["conflicts"].append(
                {
                    "person_id": op.person_id,
                    "record_id": op.record_id,
                    "platform": op.platform,
                    "value": op.value,
                    **op.detail,
                }
            )
        else:
            summary["planned"][op.kind] = summary["planned"].get(op.kind, 0) + 1
    summary["ops"] = [
        {
            "kind": o.kind,
            "person_id": o.person_id,
            "record_id": o.record_id,
            "platform": o.platform,
            "value": o.value,
        }
        for o in ops
        if o.kind != "conflict"
    ]
    if apply_writes:
        summary["applied"] = apply(ops)
    return summary
