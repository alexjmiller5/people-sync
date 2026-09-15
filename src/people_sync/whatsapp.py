"""WhatsApp evidence from an operator-supplied metadata snapshot.

The snapshot is opened read-only and immutable; only chat-session, push-name and
cached-picture metadata are read, never message bodies. Identity is the native
opaque `@lid` when the snapshot has one; a phone-only chat gets a random id kept
in a 0600 private map, so no phone-form JID or media path ever reaches a capture.
Names pass the shared export value check (a phone typed as a name is excluded,
not retained). Cached pictures are embedded as bytes so the capture replays
offline. Records stay pending: the matcher never links WhatsApp by name.
"""

import base64
import hashlib
import json
import re
import secrets
import sqlite3
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from people_sync import captures, ledger, lifedata, photos, sources
from people_sync.scrape.profile import Profile, upsert_profiles

SOURCE = "whatsapp"
FORMAT = "whatsapp-snapshot-v1"
POLICY = (
    "whatsapp-snapshot-filter-v1: active direct chats only; native opaque LID or a local "
    "random id, session and push names passing the export value check, last-message time, "
    "cached picture bytes and metadata; excluded: self, group/status/broadcast/community "
    "rows, phone-form JIDs, media paths, message bodies"
)
TABLES = {"ZWACHATSESSION", "ZWAPROFILEPICTUREITEM", "ZWAPROFILEPUSHNAME"}
SESSION_TYPES = {1: "group", 2: "broadcast", 3: "status", 4: "community"}
EXCLUDED = ("self", "group", "status", "broadcast", "community", "removed", "other", "names")
PHOTO_STATUSES = {"retained", "no-path", "missing-file", "refused-path", "invalid-image"}
COCOA = datetime(2001, 1, 1, tzinfo=timezone.utc)
_SIGNATURES = {b"\xff\xd8\xff": "jpeg", b"\x89PNG\r\n\x1a\n": "png"}
_LID = re.compile(r"[0-9a-z]+@lid")
_COUNTERPART = {
    "ordinal": "count",
    "source_id": "whatsapp-id",
    "id_kind": "text",
    "partner_name": "text",
    "push_name": "text",
    "last_message_epoch": "epoch",
    "contact_refs": ["contact-ref"],  # optional: ledger ids of contacts sharing the number
}
_REQUIRED = set(_COUNTERPART) - {"contact_refs"}
_PHOTO = {
    "picture_id": "opaque-id",
    "request_epoch": "epoch",
    "status": "text",
    "resolution": "text",
    "format": "text",
    "sha256": "sha256",
    "bytes": "count",
    "data": "base64",
}


def _image_format(data: bytes) -> str | None:
    return next((fmt for sig, fmt in _SIGNATURES.items() if data.startswith(sig)), None)


def validate_payload(payload: dict) -> None:
    """Reject unknown fields, phone-form ids and inconsistent picture bytes."""
    r = captures._require
    r(set(payload) == {"format", "complete", "excluded", "counterparts"})
    r(payload["format"] == FORMAT and payload["complete"] is True)
    r(set(payload["excluded"]) == set(EXCLUDED))
    sources._shape(payload["excluded"], dict.fromkeys(EXCLUDED, "count"))
    r(isinstance(payload["counterparts"], list))
    seen = set()
    for index, c in enumerate(payload["counterparts"]):
        r(_REQUIRED | {"photo"} <= set(c) <= set(_COUNTERPART) | {"photo"})
        sources._shape({k: v for k, v in c.items() if k != "photo"}, _COUNTERPART)
        r(len(c.get("contact_refs", [])) == len(set(c.get("contact_refs", []))))
        r(c["ordinal"] == index and c["id_kind"] in {"lid", "local"})
        r(c["source_id"].startswith(c["id_kind"] + "-") and c["source_id"] not in seen)
        seen.add(c["source_id"])
        if c["photo"] is None:
            continue
        p = sources._shape(c["photo"], _PHOTO)
        r(p["status"] in PHOTO_STATUSES)
        if p["status"] != "retained":
            r(set(p) == {"picture_id", "request_epoch", "status"})
            continue
        r(set(p) == set(_PHOTO) and p["resolution"] in {"thumbnail", "full"})
        data = base64.b64decode(p["data"], validate=True)
        r(hashlib.sha256(data).hexdigest() == p["sha256"] and len(data) == p["bytes"])
        r(_image_format(data) == p["format"])


def _local_ids(state_dir):
    """Random opaque ids for phone-only chats; the phone→id map never leaves this file."""
    path = captures.state_directory(state_dir) / "whatsapp-ids.json"
    ids = json.loads(path.read_text()) if path.exists() else {}

    def get(jid: str) -> str:
        if jid not in ids:
            ids[jid] = secrets.token_hex(16)
            captures.write_private(path, captures.encode(ids))
            path.chmod(0o600)
        return ids[jid]

    return get


def _photo(items, media_root: Path) -> dict | None:
    if not items:
        return None
    request, path, picture_id = max(items, key=lambda item: item[0] or 0)
    photo = {"picture_id": picture_id, "request_epoch": request, "status": "no-path"}
    if not path:
        return photo
    photo["status"] = "missing-file"
    for candidate, resolution in ((path, "full"), (path + ".thumb", "thumbnail")):
        target = media_root / candidate
        if not target.exists():
            continue
        if not target.resolve().is_relative_to(media_root):
            photo["status"] = "refused-path"
            return photo
        data = target.read_bytes()
        fmt = _image_format(data)
        if fmt is None:
            photo["status"] = "invalid-image"
            return photo
        photo.update(
            status="retained",
            resolution=resolution,
            format=fmt,
            sha256=hashlib.sha256(data).hexdigest(),
            bytes=len(data),
            data=base64.b64encode(data).decode(),
        )
        return photo
    return photo


def contact_lookup():
    """digits -> ledger record ids of the address-book contacts holding that number.
    Reads the local address book and the Google ledger; the number itself is used
    in memory only."""
    index = sources.phone_index()
    google = sources.google_contact_ids()

    def lookup(digits: str) -> list[str]:
        refs = set()
        for hit in index.get(digits, []):
            refs.add(f"apple_contacts:{hit['apple']}")
            if hit.get("external") in google:
                refs.add(google[hit["external"]])
        return sorted(refs)

    return lookup


def _digits(jid: str) -> str:
    return re.sub(r"\D", "", jid.split("@")[0])[-10:]


def collect(snapshot_path, media_dir, *, state_dir=None, self_id=None, contacts=None) -> dict:
    """Read the snapshot and build a validated, privacy-filtered capture (no writes).
    `contacts(digits)` may return ledger record ids of contacts sharing a phone
    number; those pointers are retained, the number never is."""
    if not self_id:
        raise ValueError("self identity required: pass the native own JID explicitly")
    media_root = Path(media_dir).resolve()
    uri = f"file:{Path(snapshot_path).resolve()}?mode=ro&immutable=1"
    db = sqlite3.connect(uri, uri=True)
    try:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not TABLES <= tables:
            raise ValueError("unknown snapshot schema")
        sessions = db.execute(
            "SELECT ZREMOVED, ZSESSIONTYPE, ZCONTACTIDENTIFIER, ZCONTACTJID, ZPARTNERNAME, "
            "ZLASTMESSAGEDATE FROM ZWACHATSESSION ORDER BY Z_PK"
        ).fetchall()
        push = {
            jid: name for jid, name in db.execute("SELECT ZJID, ZPUSHNAME FROM ZWAPROFILEPUSHNAME")
        }
        pictures = defaultdict(list)
        for jid, path, picture_id, request in db.execute(
            "SELECT ZJID, ZPATH, ZPICTUREID, ZREQUESTDATE FROM ZWAPROFILEPICTUREITEM"
        ):
            pictures[jid].append((request, path, picture_id))
    finally:
        db.close()

    excluded = dict.fromkeys(EXCLUDED, 0)
    local_id = _local_ids(state_dir)

    def safe_name(text):
        if not text:
            return None
        try:
            captures._check_export_value(text)
            return text
        except ValueError:
            excluded["names"] += 1
            return None

    counterparts = []
    for removed, kind, identifier, jid, name, last in sessions:
        jids = [j for j in (identifier, jid) if j]
        if removed:
            excluded["removed"] += 1
            continue
        if kind != 0:
            excluded[SESSION_TYPES.get(kind, "other")] += 1
            continue
        if self_id in jids:
            excluded["self"] += 1
            continue
        lids = [j for j in jids if _LID.fullmatch(j)]
        if lids:
            source_id, id_kind = "lid-" + lids[0].removesuffix("@lid"), "lid"
        elif jids:
            source_id, id_kind = "local-" + local_id(jids[0]), "local"
        else:
            excluded["other"] += 1
            continue
        phones = [_digits(j) for j in jids if j.endswith("@s.whatsapp.net")]
        refs = (
            sorted({ref for d in phones if len(d) >= 7 for ref in contacts(d)}) if contacts else []
        )
        counterparts.append(
            {
                "ordinal": len(counterparts),
                "source_id": source_id,
                "id_kind": id_kind,
                "partner_name": safe_name(name),
                "push_name": safe_name(next((push[j] for j in jids if j in push), None)),
                "last_message_epoch": last,
                "contact_refs": refs,
                "photo": _photo([i for j in jids for i in pictures.get(j, [])], media_root),
            }
        )
    payload = {
        "format": FORMAT,
        "complete": True,
        "excluded": excluded,
        "counterparts": counterparts,
    }
    return captures.build_capture(
        SOURCE, "contacts", payload, completeness="privacy-filtered", exclusions=[POLICY]
    )


def _iso(epoch) -> str | None:
    if epoch is None:
        return None
    stamp = COCOA + timedelta(seconds=epoch)
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_records(payload: dict) -> list[ledger.Record]:
    """Pure: the same records from the same retained payload, online or offline."""
    validate_payload(payload)
    return [
        ledger.Record(
            SOURCE,
            c["source_id"],
            None,
            c["partner_name"] or c["push_name"],
            {
                "partner_name": c["partner_name"],
                "push_name": c["push_name"],
                "last_message_at": _iso(c["last_message_epoch"]),
                "id_kind": c["id_kind"],
                "contact_refs": c.get("contact_refs", []),
            },
        )
        for c in payload["counterparts"]
    ]


def _profiles(payload: dict):
    for c in payload["counterparts"]:
        profile = Profile(
            record_id=f"{SOURCE}:{c['source_id']}",
            platform=SOURCE,
            platform_id=c["source_id"],
            display_name=c["partner_name"] or c["push_name"],
        )
        yield profile, c["photo"]


def ingest(snapshot_path, media_dir, *, self_id=None, state_dir=None, contacts=None) -> dict:
    """Retain the capture and every picture, then write pending evidence."""
    capture = collect(
        snapshot_path, media_dir, state_dir=state_dir, self_id=self_id, contacts=contacts
    )
    key = captures.retain(capture, state_dir=state_dir)
    payload = capture["payload"]
    records = parse_records(payload)
    for record in records:
        record.capture_key = key

    prior = {
        row["record_id"]: row
        for row in lifedata.sql(
            "SELECT record_id, avatar_r2_key, avatar_sha256 FROM people_sync_profiles "
            f"WHERE platform = {lifedata.sq(SOURCE)} AND deleted_at IS NULL"
        )
    }
    avatars, uploaded = {}, 0
    for profile, photo in _profiles(payload):
        existing = prior.get(profile.record_id, {})
        avatars[profile.record_id] = (existing.get("avatar_r2_key"), existing.get("avatar_sha256"))
        if (
            photo
            and photo["status"] == "retained"
            and photo["sha256"] != existing.get("avatar_sha256")
        ):
            image = base64.b64decode(photo["data"])
            ext = "jpg" if photo["format"] == "jpeg" else "png"
            avatar_key = f"photos/records/{SOURCE}/{profile.platform_id}-{photo['sha256']}.{ext}"
            photos.put_object(avatar_key, image, content_type=f"image/{photo['format']}")
            avatars[profile.record_id] = (avatar_key, photo["sha256"])
            uploaded += 1

    report = ledger.upsert(records)
    held = {h["record_id"] for h in report.get("held", [])}
    upsert_profiles(
        [
            (profile, *avatars[profile.record_id], key)
            for profile, _ in _profiles(payload)
            if profile.record_id not in held
        ]
    )
    return report | {
        "counterparts": len(records),
        "photos": uploaded,
        "excluded": payload["excluded"],
    }
