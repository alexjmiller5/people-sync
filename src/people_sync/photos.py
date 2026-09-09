"""Content-addressed profile photos and retained originals through life-data.

Only the hub URL and a scoped file token are needed. Photo history is
append-only: identical bytes for a person are skipped, changed pictures
get a new row. Existing object keys remain stable.
"""

import base64
import hashlib
import json
import os
from urllib.parse import quote

import httpx
import structlog

from people_sync import lifedata, sources

log = structlog.get_logger(__name__)


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {os.environ['LIFE_HUB_TOKEN']}",
        "User-Agent": "people-sync/0.1",
    }


def _url(key: str) -> str:
    return f"{os.environ['LIFE_HUB_URL'].rstrip('/')}/v1/files/{quote(key, safe='/')}"


def _upload(key: str, data: bytes, content_type: str | None = None) -> None:
    headers = _headers()
    if content_type:
        headers["Content-Type"] = content_type
    resp = httpx.put(_url(key), headers=headers, content=data, timeout=60)
    resp.raise_for_status()


def put_object(key: str, data: bytes, content_type: str | None = None) -> None:
    """Store a retained original in this client's authorized namespace."""
    _upload(key, data, content_type)


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
    existing = lifedata.sql(
        "SELECT id FROM person_photos WHERE deleted_at IS NULL "
        f"AND person_id = {lifedata.sq(person_id)} AND sha256 = {lifedata.sq(sha)}"
    )
    if existing:
        return None
    key = f"photos/people/{person_id}/{platform}-{sha[:8]}.{ext}"
    _upload(key, image)
    lifedata.insert(
        "person_photos",
        [
            {
                "person_id": person_id,
                "platform": platform,
                "r2_key": key,
                "sha256": sha,
                "fetched_at": lifedata.now_iso(),
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
