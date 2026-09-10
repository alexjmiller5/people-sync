"""Build a private, offline review page from prepared evidence snapshots.

Run with --batch LABEL context.json (repeatable), --photos manifest.json,
and --output /private/path/index.html. Photo paths in the manifest are
relative to that output file. This script neither fetches nor writes life-data.
"""

import argparse
import hashlib
import json
from pathlib import Path
import re


def build_page(batches: list[tuple[str, Path]], photos: dict[str, str]) -> str:
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
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    page = build_page(args.batch, json.loads(args.photos.read_text()))
    args.output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    args.output.write_text(page)
    args.output.chmod(0o600)
    print(f"Review page written: {args.output}")


if __name__ == "__main__":
    main()
