"""Content-addressed profile-photo storage in Cloudflare R2, with sha256 dedupe.

Upload mechanism: the Cloudflare v4 REST R2 object API
(PUT/GET/DELETE /accounts/{account_id}/r2/buckets/{bucket}/objects/{key}),
authenticated with the same CF API token used elsewhere in the estate - no
S3-compatible key/secret pair needed. The account id is never hardcoded; it's
derived at runtime from the token via GET /accounts.

Photo history is append-only: store_photo only skips the upload+insert when
the exact bytes (by sha256) were already stored for that person. A changed
picture is a new row; old rows are never overwritten or deleted here.
"""

import base64
import hashlib
import json
import os

import httpx
import structlog

from contact_sync import lifedata, sources

log = structlog.get_logger(__name__)

_CF_API = "https://api.cloudflare.com/client/v4"
_BUCKET = os.environ.get("CF_R2_BUCKET", "life-data-archive")


def _cf_headers() -> dict:
    return {"Authorization": f"Bearer {os.environ['CF_API_TOKEN']}"}


def _account_id() -> str:
    resp = httpx.get(f"{_CF_API}/accounts", headers=_cf_headers(), timeout=30)
    resp.raise_for_status()
    return resp.json()["result"][0]["id"]


def _upload(key: str, data: bytes) -> None:
    url = f"{_CF_API}/accounts/{_account_id()}/r2/buckets/{_BUCKET}/objects/{key}"
    resp = httpx.put(url, headers=_cf_headers(), content=data, timeout=60)
    resp.raise_for_status()


def store_photo(person_id: str, platform: str, image: bytes, ext: str) -> str | None:
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


def fetch_url_photo(url: str) -> bytes | None:
    try:
        resp = httpx.get(url, timeout=30, follow_redirects=True)
    except httpx.HTTPError:
        log.warning("photo fetch failed", source="url", index=-1, reason="request failed")
        return None
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
