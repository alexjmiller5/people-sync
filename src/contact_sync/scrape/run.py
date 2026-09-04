"""Platform-agnostic scrape loop.

Selects stale/never-scraped ledger records for a platform, then per record:
paces, navigates, checks for a challenge page, runs the platform's extractor,
archives the raw page + captured XHRs to R2, fetches and dedupes the avatar,
and upserts `contact_profiles`. A platform plugs in by exposing `URL`,
`CAPTURE`, `EXTRACTOR_JS`, and `parse(eval_result, captured) -> Profile` -
see `instagram.py`.
"""

import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from importlib import import_module

import structlog
from websockets.exceptions import ConnectionClosed

from contact_sync import lifedata, photos
from contact_sync.scrape.cdp import Browser, CdpError
from contact_sync.scrape.pace import DEFAULT_STATE_PATH, Pacer, is_challenge
from contact_sync.scrape.profile import ExtractError, upsert_profile

# A CDP protocol error or a dropped websocket means the browser session
# itself is gone - halt rather than spin through the remaining records.
_BROWSER_LOST = (CdpError, ConnectionClosed)

log = structlog.get_logger(__name__)

STALE_DAYS = 180
NAV_WAIT_MS = 12000
PAGE_TEXT_JS = "document.title + '\\n' + document.body.innerText.slice(0,3000)"


def _stale_cutoff() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    return cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _records_sql(platform: str, cutoff: str) -> str:
    return (
        "SELECT c.id, c.handle, c.name, "
        "p.avatar_r2_key AS avatar_r2_key, p.avatar_sha256 AS avatar_sha256 "
        "FROM contact_records c "
        "LEFT JOIN contact_profiles p ON p.record_id = c.id "
        f"WHERE c.source = {lifedata.sq(platform)} "
        "AND c.deleted_at IS NULL "
        "AND c.status IN ('pending', 'matched') "
        f"AND (p.record_id IS NULL OR p.scraped_at < {lifedata.sq(cutoff)}) "
        "ORDER BY c.first_seen"
    )


def _select_records(platform: str) -> list[dict]:
    return lifedata.sql(_records_sql(platform, _stale_cutoff()))


def _record_key(record_id: str) -> str:
    return record_id.replace(":", "_").replace("/", "_")


def _ext_from_url(url: str) -> str:
    ext = os.path.splitext(url.split("?", 1)[0])[1].lstrip(".").lower()
    return ext or "jpg"


def _fetch_avatar_via_page(browser: Browser, url: str) -> bytes | None:
    """Fallback for CDNs that reject a plain httpx GET: fetch the bytes from
    inside the page (which already carries the right cookies/referrer) and
    hand them back base64-encoded."""
    js = (
        "(async () => { try { "
        f"const r = await fetch({json.dumps(url)}); "
        "const buf = await r.arrayBuffer(); const bytes = new Uint8Array(buf); "
        "let bin = ''; for (let i = 0; i < bytes.byteLength; i++) bin += String.fromCharCode(bytes[i]); "
        "return btoa(bin); "
        "} catch (e) { return null; } })()"
    )
    b64 = browser.eval(js)
    return base64.b64decode(b64) if b64 else None


def _resolve_avatar(
    browser: Browser,
    platform: str,
    index: int,
    record_key: str,
    avatar_url: str | None,
    existing_key: str | None,
    existing_sha: str | None,
) -> tuple[str | None, str | None]:
    if not avatar_url:
        return existing_key, existing_sha

    image = photos.fetch_url_photo(avatar_url)
    source = "direct"
    if image is None:
        image = _fetch_avatar_via_page(browser, avatar_url)
        source = "page-fetch"
    if image is None:
        log.warning(
            "avatar fetch failed", platform=platform, index=index, reason="direct and page-fetch"
        )
        return existing_key, existing_sha

    sha = hashlib.sha256(image).hexdigest()
    if sha == existing_sha:
        return existing_key, existing_sha

    ext = _ext_from_url(avatar_url)
    key = f"photos/records/{platform}/{record_key}-{sha[:8]}.{ext}"
    photos.put_object(key, image, content_type=f"image/{ext}")
    log.info("avatar stored", platform=platform, index=index, reason=source)
    return key, sha


def scrape(
    platform: str,
    max_n: int | None = None,
    state_path: str = DEFAULT_STATE_PATH,
) -> dict:
    module = import_module(f"contact_sync.scrape.{platform}")
    pacer = Pacer(platform, state_path=state_path)
    records = _select_records(platform)
    if max_n is not None:
        records = records[:max_n]

    done = 0
    skipped = 0
    halted: str | None = None
    browser = Browser.connect()
    try:
        for index, record in enumerate(records):
            if not pacer.allow():
                halted = "daily cap reached"
                log.info("scrape halted", platform=platform, index=index, reason=halted)
                break

            handle = record.get("handle")
            if not handle:
                skipped += 1
                continue

            try:
                url = module.URL.format(handle=handle)
                nav_result = browser.navigate(url, NAV_WAIT_MS, capture=module.CAPTURE)
                captured = nav_result.get("captured", [])

                page_text = browser.eval(PAGE_TEXT_JS) or ""
                if is_challenge(page_text):
                    halted = "challenge page"
                    log.warning("scrape halted", platform=platform, index=index, reason=halted)
                    break

                raw_eval = browser.eval(module.EXTRACTOR_JS)
                eval_result = json.loads(raw_eval) if isinstance(raw_eval, str) else raw_eval
                profile = module.parse(eval_result, captured)
                profile.record_id = record["id"]

                record_key = _record_key(record["id"])
                raw_key = f"profiles/{platform}/{record_key}/{lifedata.now_iso()}.json"
                photos.put_object(
                    raw_key,
                    json.dumps({"eval": eval_result, "captured": captured}).encode(),
                    content_type="application/json",
                )

                avatar_key, avatar_sha = _resolve_avatar(
                    browser,
                    platform,
                    index,
                    record_key,
                    profile.avatar_url,
                    record.get("avatar_r2_key"),
                    record.get("avatar_sha256"),
                )

                upsert_profile(profile, avatar_key, avatar_sha, raw_key)
            except _BROWSER_LOST:
                halted = "browser lost"
                log.error("scrape halted", platform=platform, index=index, reason=halted)
                break
            except Exception as e:
                reason = str(e) if isinstance(e, ExtractError) else type(e).__name__
                log.warning("record failed", platform=platform, index=index, reason=reason)
                skipped += 1
                time.sleep(pacer.next_gap())
                continue

            pacer.record()
            done += 1
            log.info("profile scraped", platform=platform, index=index)
            time.sleep(pacer.next_gap())
    finally:
        browser.close()

    return {"done": done, "skipped": skipped, "halted": halted}
