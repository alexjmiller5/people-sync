"""Propose identity clusters for the private review page.

Input: the review contexts (name group -> current_people, google_candidates,
profiles). Output: proposals keyed the way `review` expects. Entries join a
cluster only on a concrete shared signal - the same full name, a handle that
spells a name, a matching surname with a small typo, a shared place/school/era
cue; everything else is proposed as a separate person (the user's default),
never left as "unresolved". Names and photos surface candidates; the user
decides.
"""

import argparse
import itertools
import json
import re
import unicodedata
from pathlib import Path

LINK_SCORE = 3  # a full-name or handle match on its own
WEAK_LINK_SCORE = 2  # a fuzzy surname or a cue match, only when unambiguous

# Era / community circles that imply a place or school the profiles may name.
ERA = re.compile(r"^(?:fall|spring|summer|winter) 20(?:2[1-5])$", re.I)
CUE_ALIASES = {
    "boston university": "bu",
    "@bualumni": "bu",
    "bu alumni": "bu",
    "terriers": "bu",
    "boston": "boston",
    "bos": "boston",
    "westport": "westport",
    "staples": "westport",
    "staples high school": "westport",
    "new york": "nyc",
    "nyc": "nyc",
    "new york city": "nyc",
    "madrid": "madrid",
    "primavera": "madrid",
    "apogee": "apogee",
    "fidelity": "fidelity",
    "sae": "sae",
    "sigma alpha epsilon": "sae",
    "capital one": "capital one",
}
CIRCLE_CUES = {
    "primavera 2024": {"madrid", "bu"},
    "high school": {"westport"},
    "staples": {"westport"},
    "westport": {"westport"},
    "bu": {"bu", "boston"},
    "boston": {"boston"},
    "nyc": {"nyc"},
}


def letters(s: str | None) -> str:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    return "".join(c for c in s if c.isalpha() and not unicodedata.combining(c))


def words(s: str | None) -> list[str]:
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(c for c in s if not unicodedata.combining(c))
    return [w for w in re.split(r"[^a-z]+", s) if w]


def edit_distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def cue_tokens(*texts) -> set[str]:
    found = set()
    for text in texts:
        if not text:
            continue
        if isinstance(text, (list, dict)):
            text = json.dumps(text, ensure_ascii=False)
        low = unicodedata.normalize("NFKD", str(text).casefold())
        for phrase, cue in CUE_ALIASES.items():
            if re.search(r"(?<![a-z])" + re.escape(phrase) + r"(?![a-z])", low):
                found.add(cue)
    return found


class Item:
    def __init__(self, kind, ident, name, *, handle=None, platform=None, texts=(), circles=()):
        self.kind = kind  # "person" | "google" | "profile"
        self.id = ident
        self.name = name or ""
        self.handle = handle
        self.platform = platform
        self.tokens = words(self.name)
        self.first = self.tokens[0] if self.tokens else ""
        self.last = self.tokens[-1] if len(self.tokens) > 1 else ""
        self.full = letters(self.name) if len(self.tokens) > 1 else ""
        self.handle_letters = letters(handle) if handle else ""
        self.cues = cue_tokens(*texts)
        for circle in circles:
            key = str(circle).casefold()
            self.cues |= CIRCLE_CUES.get(key, set())
            if ERA.match(key):
                self.cues |= {"bu", "boston"}
        if "bu" in self.cues:
            self.cues.add("boston")

    @property
    def label(self) -> str:
        return self.name or self.handle or self.id


def items_for(group: dict, google_groups: dict | None, cues: dict | None) -> list[Item]:
    google_groups = google_groups or {}
    cues = cues or {}
    out = []
    for p in group.get("current_people", []):
        circles = p.get("circles")
        circles = json.loads(circles) if isinstance(circles, str) else (circles or [])
        out.append(
            Item(
                "person",
                p["id"],
                p.get("name"),
                texts=(p.get("notes"), *cues.get(p["id"], [])),
                circles=circles,
            )
        )
    for c in group.get("google_candidates", []):
        raw = c.get("raw")
        raw = json.loads(raw) if isinstance(raw, str) else (raw or {})
        labels = [
            google_groups.get(m.get("contactGroupMembership", {}).get("contactGroupResourceName"))
            for m in raw.get("labels", [])
        ]
        orgs = [o.get("name") for o in raw.get("org", [])]
        out.append(
            Item(
                "google",
                c["id"],
                c.get("name"),
                texts=(*orgs, *[name for name in labels if name], *cues.get(c["id"], [])),
                circles=[name for name in labels if name] + [o for o in orgs if o],
            )
        )
    for p in group.get("profiles", []):
        out.append(
            Item(
                "profile",
                p["record_id"],
                p.get("display_name") or p.get("source_name"),
                handle=p.get("handle"),
                platform=p.get("platform"),
                texts=(
                    p.get("bio"),
                    p.get("location"),
                    p.get("hometown"),
                    p.get("education"),
                    p.get("work"),
                    *cues.get(p["record_id"], []),
                ),
            )
        )
    return out


def signal(a: Item, b: Item) -> tuple[int, list[str]]:
    """Score and reasons for treating a and b as one person."""
    score, why = 0, []
    if a.full and a.full == b.full:
        score, why = score + 3, why + ["same full name"]
    elif a.first and a.first == b.first and a.last and b.last:
        d = edit_distance(a.last, b.last)
        if 0 < d <= 2 and min(len(a.last), len(b.last)) >= 5:
            score, why = score + 2, why + [f"surname spelled {a.last!r} vs {b.last!r}"]
    for x, y in ((a, b), (b, a)):
        if x.handle_letters and y.full and len(y.full) >= 8:
            if x.handle_letters == y.full or y.full in x.handle_letters:
                score, why = score + 3, why + [f"handle {x.handle!r} spells {y.name!r}"]
                break
            if edit_distance(x.handle_letters, y.full) <= 2:
                score, why = score + 2, why + [f"handle {x.handle!r} nearly spells {y.name!r}"]
                break
    if a.handle_letters and b.handle_letters and a.handle_letters == b.handle_letters:
        score, why = score + 3, why + ["same handle letters"]
    if a.first and a.first == b.first:
        shared = a.cues & b.cues
        if shared:
            score, why = score + 2, why + [f"shared cue: {', '.join(sorted(shared))}"]
    return score, why


def cluster(items: list[Item]) -> list[tuple[list[Item], list[str], bool]]:
    """Greedy union of the strongest signals first; returns (members, reasons, fuzzy)."""
    parent = {i.id: i.id for i in items}
    reasons: dict[str, list[str]] = {i.id: [] for i in items}
    fuzzy: dict[str, bool] = {i.id: False for i in items}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    pairs = []
    for a, b in itertools.combinations(items, 2):
        s, why = signal(a, b)
        if s:
            pairs.append((s, a, b, why))
    pairs.sort(key=lambda p: -p[0])
    for s, a, b, why in pairs:
        if s < WEAK_LINK_SCORE:
            continue
        ra, rb = find(a.id), find(b.id)
        if ra == rb:
            continue
        if s < LINK_SCORE:
            # a weak signal only joins when neither side has a candidate outside
            # the two clusters being joined (a rival that is already inside is fine)
            rivals = [
                p
                for p in pairs
                if p[0] >= WEAK_LINK_SCORE
                and (p[1] in (a, b) or p[2] in (a, b))
                and {find(p[1].id), find(p[2].id)} - {ra, rb}
            ]
            if rivals:
                continue
        members = [i for i in items if find(i.id) in (ra, rb)]
        same_platform = [
            i.platform for i in members if i.kind == "profile" and i.platform is not None
        ]
        if len(same_platform) != len(set(same_platform)) and "same handle letters" not in why:
            continue  # one person rarely holds two accounts on the same platform
        parent[rb] = ra
        reasons[ra] = reasons[ra] + reasons[rb] + why
        weak = s < LINK_SCORE or any("nearly" in w or w.startswith("surname spelled") for w in why)
        fuzzy[ra] = fuzzy[ra] or fuzzy[rb] or weak
    groups: dict[str, list[Item]] = {}
    for i in items:
        groups.setdefault(find(i.id), []).append(i)
    return [(members, reasons[root], fuzzy[root]) for root, members in groups.items()]


def label_for(members: list[Item]) -> str:
    named = sorted(members, key=lambda i: (i.kind != "google", i.kind != "person", -len(i.full)))
    base = next((i.name for i in named if len(i.tokens) > 1), None) or named[0].label
    cues = set().union(*(i.cues for i in members))
    return base + (f" - {', '.join(sorted(cues))}" if cues else "")


def propose_group(group: dict, google_groups=None, cues=None) -> dict:
    items = items_for(group, google_groups, cues)
    clusters = []
    for members, why, fuzzy in cluster(items):
        entry = {
            "label": label_for(members),
            "reason": (
                "; ".join(dict.fromkeys(why)).capitalize() + "."
                if why
                else "No shared name, handle or cue with any other entry; proposed as a separate person."
            ),
            "person_ids": [i.id for i in members if i.kind == "person"],
            "record_ids": [i.id for i in members if i.kind != "person"],
        }
        if fuzzy:
            entry["uncertainty"] = "Joined on a spelling or cue match only; check the photos."
        clusters.append(entry)
    return {"clusters": clusters}


def propose(batches, google_groups=None, cues=None) -> dict:
    out = {}
    for batch, path in batches:
        for name, group in json.loads(Path(path).read_text()).items():
            out[json.dumps([batch, name], ensure_ascii=False)] = propose_group(
                group, google_groups, cues
            )
    return out


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="people-sync propose", description=__doc__)
    parser.add_argument(
        "--batch", nargs=2, action="append", required=True, metavar=("LABEL", "JSON")
    )
    parser.add_argument("--google-groups", type=Path, help="{contactGroups/<id>: label} JSON")
    parser.add_argument("--cues", type=Path, help="{person or record id: [cue text, ...]} JSON")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    result = propose(
        args.batch,
        json.loads(args.google_groups.read_text()) if args.google_groups else None,
        json.loads(args.cues.read_text()) if args.cues else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=1))
    args.output.chmod(0o600)
    print(f"Proposals written: {args.output} ({len(result)} groups)")
