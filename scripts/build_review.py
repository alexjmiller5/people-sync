"""Build a private, offline review page from prepared evidence snapshots.

Run with --batch LABEL context.json (repeatable), --photos manifest.json,
optional --proposals proposals.json, and --output /private/path/index.html.
Photo paths in the manifest are
relative to that output file. This script neither fetches nor writes life-data.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re


def attach_proposal(group: dict, proposal: dict | None) -> None:
    available = {
        "person_ids": dict.fromkeys(p["id"] for p in group["current_people"]),
        "record_ids": dict.fromkeys(
            [c["id"] for c in group["google_candidates"]]
            + [p["record_id"] for p in group["profiles"]]
        ),
    }
    assigned = {field: set() for field in available}
    if proposal is not None:
        if not isinstance(proposal, dict) or not isinstance(proposal.get("clusters"), list):
            raise ValueError("Proposal must contain a clusters list")
        if "question" in proposal and not isinstance(proposal["question"], str):
            raise ValueError("Proposal question must be a string")
        for cluster in proposal["clusters"]:
            if not isinstance(cluster, dict):
                raise ValueError("Proposal cluster must be an object")
            for field in ("label", "reason", "uncertainty"):
                if field == "uncertainty" and field not in cluster:
                    continue
                if not isinstance(cluster.get(field), str):
                    raise ValueError(f"Cluster {field} must be a string")
            for field, ids in available.items():
                refs = cluster.get(field)
                if not isinstance(refs, list) or any(not isinstance(r, str) for r in refs):
                    raise ValueError(f"Cluster {field} must be a list of strings")
                for ref in refs:
                    if ref not in ids:
                        raise ValueError(f"Unknown cluster reference in {field}")
                    if ref in assigned[field]:
                        raise ValueError(f"Duplicate cluster assignment in {field}")
                    assigned[field].add(ref)
            if not cluster["person_ids"] and not cluster["record_ids"]:
                raise ValueError("Empty proposal cluster")
    group["proposal"] = proposal
    group["proposal_id"] = (
        hashlib.sha256(json.dumps(proposal, sort_keys=True).encode()).hexdigest()
        if proposal is not None
        else None
    )
    group["unresolved"] = {
        field: [ref for ref in ids if ref not in assigned[field]]
        for field, ids in available.items()
    }


def build_page(
    batches: list[tuple[str, Path]], photos: dict[str, str], proposals: dict | None = None
) -> str:
    groups = []
    for batch, path in batches:
        for name, group in json.loads(Path(path).read_text()).items():
            for field in ("current_people", "google_candidates", "profiles"):
                if not isinstance(group.get(field), list):
                    raise ValueError(f"Missing review list: {field}")
            groups.append(
                {
                    **group,
                    "name": name,
                    "batch": batch,
                    "key": json.dumps([batch, name], ensure_ascii=False),
                }
            )
    for path in photos.values():
        if not isinstance(path, str) or not re.fullmatch(
            r"photos/[a-zA-Z0-9_-]+\.[a-zA-Z0-9]+", path
        ):
            raise ValueError("Unsafe photo path")
    snapshot = hashlib.sha256(json.dumps(groups, sort_keys=True).encode()).hexdigest()
    if proposals is None:
        proposals = {}
    if not isinstance(proposals, dict) or proposals.keys() - {g["key"] for g in groups}:
        raise ValueError("Proposals must be an object keyed by exact review group keys")
    for group in groups:
        if group["key"] in proposals and not isinstance(proposals[group["key"]], dict):
            raise ValueError("Proposal must be an object")
        attach_proposal(group, proposals.get(group["key"]))
    data = {"snapshot_id": snapshot, "groups": groups, "photos": photos}
    encoded = (
        json.dumps(data, ensure_ascii=False)
        .replace("&", "\\u0026")
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
    )
    return Path(__file__).with_name("review.html").read_text().replace("__REVIEW_DATA__", encoded)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--batch", nargs=2, action="append", required=True, metavar=("LABEL", "JSON")
    )
    parser.add_argument("--photos", type=Path, required=True)
    parser.add_argument("--proposals", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    page = build_page(
        args.batch,
        json.loads(args.photos.read_text()),
        json.loads(args.proposals.read_text()) if args.proposals else None,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(page)
    args.output.chmod(0o600)
    print(f"Review page written: {args.output}")


if __name__ == "__main__":
    main()
