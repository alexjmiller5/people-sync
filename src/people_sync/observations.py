"""Immutable addresses into retained source inputs, independent of identity resolution."""

import base64
import hashlib
from concurrent.futures import ThreadPoolExecutor

from people_sync import captures, ledger, somadata, photos, replay

TABLE = "people_sync_observations"
COLUMNS = (
    "id",
    "capture_key",
    "source",
    "kind",
    "captured_at",
    "completeness",
    "capture_sha256",
    "payload_sha256",
    "scope",
    "entry_ordinal",
    "entry_sha256",
)


def _hash(value):
    return hashlib.sha256(captures.encode(value)).hexdigest()


def _scopes(c):
    """A scope summary plus every original row slot, including excluded/failed slots."""
    p = c["payload"]
    if c["kind"] == "export":
        for file in p["files"]:
            entries = replay.export_entries(
                c["source"], base64.b64decode(file["data"], validate=True)
            )
            yield file["role"], file, list(enumerate(entries))
    elif c["kind"] == "list":
        yield p["scope"], p, list(zip(p["entry_ordinals"], p["entries"], strict=True))
    elif c["kind"] == "contacts":
        if c["source"] == "whatsapp":
            yield "counterparts", p, [(entry["ordinal"], entry) for entry in p["counterparts"]]
        else:
            collection = "pages" if c["source"] == "google_contacts" else "databases"
            for group in p[collection]:
                yield (
                    f"{collection}:{group['ordinal']}",
                    group,
                    [(entry["ordinal"], entry) for entry in group["entries"]],
                )
    elif c["kind"] == "profile":
        yield "profile", p, [(0, p)]


def plan(capture: dict) -> list[dict]:
    c = captures.validate(capture)
    key = f"profiles/{c['source']}/captures/{c['capture_id']}.json"
    common = {k: c[k] for k in ("source", "kind", "captured_at", "completeness", "payload_sha256")}
    common.update(capture_key=key, capture_sha256=_hash(c))
    rows = []
    for scope, summary, entries in _scopes(c):
        for ordinal, entry in [(None, summary), *entries]:
            rows.append(
                {
                    **common,
                    "id": "psobs:" + _hash([key, scope, ordinal]),
                    "scope": scope,
                    "entry_ordinal": ordinal,
                    "entry_sha256": _hash(entry),
                }
            )
    return rows


def _verified(item):
    c, rows = item
    try:
        return photos.get_object(rows[0]["capture_key"]) == captures.encode(c)
    except Exception:
        return False


def index(inputs: list[dict], *, apply: bool = False) -> dict:
    """Preview offline; apply only remote-verified captures, in batched Soma writes."""
    planned = {}
    for c in inputs:
        rows = plan(c)
        key = rows[0]["capture_key"]
        if key in planned and planned[key][1] != rows:
            raise ValueError("capture identity conflict")
        planned[key] = (c, rows)
    result = {
        "captures": len(planned),
        "observations": sum(len(rows) for _, rows in planned.values()),
        "applied": apply,
        "inserted": 0,
        "existing": 0,
        "held_deleted": 0,
        "failed": [],
    }
    if not apply or not planned:
        return result
    items = list(planned.values())
    with ThreadPoolExecutor(max_workers=4) as pool:
        verified = list(pool.map(_verified, items))
    rows = []
    for (c, entries), ok in zip(items, verified, strict=True):
        if ok:
            rows.extend(entries)
        else:
            result["failed"].append(
                {"capture_id": c["capture_id"], "reason": "retained-file-unverified"}
            )
    existing = {}
    for start in range(0, len(rows), ledger.CHUNK):
        ids = ",".join(somadata.sq(r["id"]) for r in rows[start : start + ledger.CHUNK])
        existing.update(
            {
                r["id"]: r
                for r in somadata.sql(
                    f"SELECT {','.join(COLUMNS)},deleted_at FROM {TABLE} WHERE id IN ({ids})"
                )
            }
        )
    missing, live = [], []
    for row in rows:
        old = existing.get(row["id"])
        if old and old.get("deleted_at"):
            result["held_deleted"] += 1
            continue
        if old and any(old.get(k) != row[k] for k in COLUMNS):
            raise ValueError("observation conflict")
        live.append(row)
        if old:
            result["existing"] += 1
        else:
            missing.append(row)
    for start in range(0, len(missing), ledger.CHUNK):
        somadata.insert(TABLE, missing[start : start + ledger.CHUNK])
    ledger.imported_from_many(TABLE, [(r["id"], r["capture_key"], ()) for r in live])
    result["inserted"] = len(missing)
    return result
