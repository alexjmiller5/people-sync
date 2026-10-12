"""Content-addressed profile photos and retained originals through soma.

Only the hub URL and a scoped file token are needed. Photo history is
append-only: identical bytes for a person are skipped, changed pictures
get a new row. Every key is a plain path a client can request
(`record_key`); `rekey` moves legacy keys that are not.
"""

import base64
import hashlib
import json
import os
import re
from urllib.parse import quote

import httpx
import structlog

from people_sync import ledger, somadata, sources

log = structlog.get_logger(__name__)


def record_key(record_id: str) -> str:
    """A ledger record's storage-key segment, [a-z0-9_-] only, so the stored key needs
    no escaping anywhere ('facebook:profile.php?id=1' -> 'facebook_id_1')."""
    slug = re.sub(r"profile\.php\?id=", "id_", record_id.lower())
    slug = re.sub(r"[^a-z0-9_-]+", "_", slug).strip("_")
    return slug or hashlib.sha256(record_id.encode()).hexdigest()


def safe_key(key: str | None) -> bool:
    """Whether a stored key is a plain path: no URL syntax, quote, escape or empty part."""
    return (
        bool(key)
        and not re.search(r"[%\\?#='\"\x00-\x1f\x7f]", key)
        and all(p not in {"", ".", ".."} for p in key.split("/"))
    )


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['SOMA_HUB_TOKEN']}",
        "User-Agent": "people-sync/0.1",
    }


def _url(key: str) -> str:
    return f"{os.environ['SOMA_HUB_URL'].rstrip('/')}/v1/files/{quote(key, safe='/')}"


def _upload(key: str, data: bytes, content_type: str | None = None) -> None:
    headers = _headers()
    if content_type:
        headers["Content-Type"] = content_type
    resp = httpx.put(_url(key), headers=headers, content=data, timeout=60)
    resp.raise_for_status()
    # The hub replies with the key it stored under; recording any other spelling
    # leaves a reference that never resolves.
    if resp.json().get("key") != key:
        raise ValueError("hub stored the object under a different key")


def put_object(key: str, data: bytes, content_type: str | None = None) -> None:
    """Store a retained original in this client's authorized namespace."""
    _upload(key, data, content_type)


class ArchiveError(RuntimeError):
    """Stop collecting when a source snapshot cannot be retained."""


def archive_profile(platform, record_id, raw_eval, captured, *, context=None) -> str:
    """Verify retention of safe profile input before interpretation.

    Exact JS strings survive only when the privacy boundary leaves them intact.
    Filtered or malformed structured input carries explicit exclusions.
    """
    from people_sync import captures
    from people_sync.scrape import snapshot

    try:
        payload = snapshot.prepare(platform, record_id, raw_eval, captured, context=context)
        return captures.retain(
            captures.build_capture(
                platform,
                "profile",
                payload,
                record_id=record_id,
                captured_at=somadata.now_iso(),
                completeness=snapshot.completeness(payload),
                exclusions=snapshot.EXCLUSIONS,
            )
        )
    except Exception:
        # Storage exceptions can contain credentials. Expose only the halt reason.
        raise ArchiveError("raw archive failed") from None


def get_object(key: str) -> bytes:
    resp = httpx.get(_url(key), headers=_headers(), timeout=60)
    resp.raise_for_status()
    return resp.content


def store_photo(person_id: str, platform: str, image: bytes, ext: str) -> str | None:
    # platform must be one of the spec's person_accounts platform values
    # (instagram, facebook, snapchat, linkedin, google_contacts,
    # apple_contacts, whatsapp, venmo, partiful, spotify) - person_photos
    # joins to person_accounts on (person_id, platform).
    sha = hashlib.sha256(image).hexdigest()
    existing = somadata.sql(
        "SELECT id FROM person_photos WHERE deleted_at IS NULL "
        f"AND person_id = {somadata.sq(person_id)} AND sha256 = {somadata.sq(sha)}"
    )
    if existing:
        return None
    key = f"photos/people/{person_id}/{platform}-{sha[:8]}.{ext}"
    _upload(key, image)
    somadata.insert(
        "person_photos",
        [
            {
                "person_id": person_id,
                "platform": platform,
                "r2_key": key,
                "sha256": sha,
                "fetched_at": somadata.now_iso(),
            }
        ],
    )
    return key


def fetch_url_photo(url: str, *, halt_on_block: bool = False) -> bytes | None:
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError:
        log.warning("photo fetch failed", source="url", index=-1, reason="request failed")
        return None
    if halt_on_block and resp.status_code in (401, 403, 429):
        resp.raise_for_status()
    if resp.status_code != 200:
        log.warning("photo fetch failed", source="url", index=-1, reason="non-200 status")
        return None
    if not resp.headers.get("content-type", "").startswith("image/"):
        log.warning("photo fetch failed", source="url", index=-1, reason="non-image content-type")
        return None
    return resp.content


def fetch_google_photo(resource_name: str) -> bytes | None:
    try:
        person = json.loads(
            sources._run(["gog", "contacts", "raw", resource_name, "-j", "--no-input"])
        )
    except (RuntimeError, ValueError):
        log.warning(
            "photo fetch failed", source="google_contacts", index=-1, reason="raw fetch failed"
        )
        return None
    url = sources._primary_or_first(person.get("photos") or [], "url")
    if not url:
        return None
    return fetch_url_photo(url)


def _extract_vcard_photo(vcard: str) -> bytes | None:
    lines = vcard.replace("\r\n", "\n").split("\n")
    b64_parts: list[str] = []
    capturing = False
    for line in lines:
        if capturing:
            if line.startswith((" ", "\t")):
                b64_parts.append(line[1:])
                continue
            break
        if line.upper().startswith("PHOTO;") or line.upper().startswith("PHOTO:"):
            _, _, data = line.partition(":")
            b64_parts.append(data)
            capturing = True
    if not b64_parts:
        return None
    try:
        return base64.b64decode("".join(b64_parts))
    except (ValueError, base64.binascii.Error):
        return None


def fetch_apple_photo(source_id: str) -> bytes | None:
    try:
        vcard = sources._run(
            [
                "osascript",
                "-e",
                f'tell application "Contacts" to get vcard of person id "{source_id}"',
            ]
        )
    except RuntimeError:
        log.warning(
            "photo fetch failed", source="apple_contacts", index=-1, reason="vcard fetch failed"
        )
        return None
    photo = _extract_vcard_photo(vcard)
    if photo is None:
        log.warning(
            "photo fetch failed", source="apple_contacts", index=-1, reason="no photo in vcard"
        )
    return photo


# Every column holding a stored photo key, with the row id and the bytes' sha256.
PHOTO_KEY_COLUMNS = (
    ("person_photos", "id", "r2_key", "sha256"),
    ("people_sync_profiles", "record_id", "avatar_r2_key", "avatar_sha256"),
)


def _safe_variant(key: str) -> str:
    """The key `record_key` makes for a legacy one: '<folder>/<record>-<sha8>.<ext>'
    with the record segment slugged, the content-hash suffix and extension kept."""
    folder, _, name = key.rpartition("/")
    stem, _, ext = name.rpartition(".")
    record, _, sha8 = stem.rpartition("-")
    return f"{folder}/{record_key(record)}-{sha8}.{ext}"


def rekey(apply_writes: bool = False) -> dict:
    """Move live photos whose key a client cannot request to the safe key: copy the
    object through the file service, verify its sha256 before and after, then repoint
    every live row with an `evidence_of` edge to the new object. Dry run by default;
    idempotent. The old object stays: this client's file grant cannot delete."""
    moves: dict[str, dict] = {}
    for table, id_col, key_col, sha_col in PHOTO_KEY_COLUMNS:
        rows = somadata.sql(
            f"SELECT {id_col} AS id, {key_col} AS key, {sha_col} AS sha256 FROM {table} "
            f"WHERE deleted_at IS NULL AND {key_col} IS NOT NULL"
        )
        for row in rows:
            if safe_key(row["key"]):
                continue
            move = moves.setdefault(
                row["key"],
                {
                    "from": row["key"],
                    "to": _safe_variant(row["key"]),
                    "sha256": row["sha256"],
                    "rows": [],
                },
            )
            if move["sha256"] != row["sha256"] or not safe_key(move["to"]):
                raise ValueError(f"cannot rekey {row['key']!r}")
            move["rows"].append({"table": table, "id": row["id"], "field": key_col})
    if not apply_writes:
        return {"applied": False, "moves": list(moves.values())}

    for move in moves.values():
        data = get_object(move["from"])
        if hashlib.sha256(data).hexdigest() != move["sha256"]:
            raise ValueError(f"{move['from']!r} does not match its sha256")
        put_object(move["to"], data, content_type=f"image/{move['to'].rpartition('.')[2]}")
        if hashlib.sha256(get_object(move["to"])).hexdigest() != move["sha256"]:
            raise ValueError(f"{move['to']!r} did not read back")

    # Edges first: a crash before the row update leaves the key unsafe, so a rerun
    # copies again (same bytes) and only updates.
    detail = json.dumps(
        {"cue": f"safe photo key migration {somadata.now_iso()[:10]}", "confidence": "high"}
    )
    asserted_by = os.environ.get("PEOPLE_SYNC_ASSERTED_BY") or "script:people-sync photos rekey"
    edges = [
        {
            "id": ledger.capture_edge_id(m["to"], r["table"], r["id"], "evidence_of", r["field"]),
            "from_kind": "takeout",
            "from_ref": m["to"],
            "to_kind": r["table"],
            "to_ref": r["id"],
            "field": r["field"],
            "rel": "evidence_of",
            "detail": detail,
            "asserted_by": asserted_by,
        }
        for m in moves.values()
        for r in m["rows"]
    ]
    if edges:
        have = {
            e["id"]
            for e in somadata.sql(
                "SELECT id FROM provenance WHERE id IN "
                f"({','.join(somadata.sq(e['id']) for e in edges)})"
            )
        }
        somadata.insert("provenance", [e for e in edges if e["id"] not in have])
    for table, id_col, key_col, _ in PHOTO_KEY_COLUMNS:
        updates = {
            r["id"]: {key_col: somadata.sq(m["to"])}
            for m in moves.values()
            for r in m["rows"]
            if r["table"] == table
        }
        if updates:
            ledger.batch_update(table, id_col, updates)
    return {"applied": True, "moves": list(moves.values())}
