"""Platform-agnostic scrape loop.

Selects stale/never-scraped ledger records for a platform, then per record:
paces, navigates, checks for a challenge page, runs the platform's extractor,
archives extractor output + captured responses before parsing, fetches and dedupes the avatar,
and upserts `people_sync_profiles`. A platform plugs in by exposing `URL`,
`CAPTURE`, `EXTRACTOR_JS`, and `parse(eval_result, captured) -> Profile` -
see `instagram.py`.
"""

import base64
import fcntl
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone
from importlib import import_module

import structlog
import httpx
from websockets.exceptions import ConnectionClosed

from people_sync import lifedata, photos
from people_sync.scrape.cdp import Browser, CdpError, ScrapeStopped
from people_sync.scrape.pace import DEFAULT_STATE_PATH, Pacer, challenge_marker
from people_sync.scrape.profile import ExtractError, upsert_profile, Profile

# A CDP protocol error or a dropped websocket means the browser session
# itself is gone - halt rather than spin through the remaining records.
_BROWSER_LOST = (CdpError, ConnectionClosed)

log = structlog.get_logger(__name__)

STALE_DAYS = 180
NAV_WAIT_MS = 12000
PAGE_TEXT_JS = "document.title + '\\n' + document.body.innerText.slice(0,3000)"
# Client-rendered profiles paint after the load event; a module's READY_JS
# names what "rendered" looks like and the loop waits for it (bounded).
READY_TIMEOUT_S = 15.0
# The extractor sentinel for "this profile no longer exists".
UNAVAILABLE = "unavailable"


def _stale_cutoff() -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=STALE_DAYS)
    return cutoff.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _records_sql(platform: str, cutoff: str) -> str:
    return (
        "SELECT c.id, c.handle, c.name, "
        "p.avatar_r2_key AS avatar_r2_key, p.avatar_sha256 AS avatar_sha256 "
        "FROM people_sync_records c "
        "LEFT JOIN people_sync_profiles p ON p.record_id = c.id "
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
        "if (!r.ok || !(r.headers.get('content-type') || '').toLowerCase().startsWith('image/')) return null; "
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

    if isinstance(browser, Browser):
        browser.check_stop()
    image = photos.fetch_url_photo(avatar_url, halt_on_block=True)
    source = "direct"
    if image is None:
        if isinstance(browser, Browser):
            browser.check_stop()
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


def _halt_screenshot(browser: Browser, platform: str, state_path: str) -> str | None:
    """What the page looked like when a run halted, next to the state file;
    None when the capture itself fails (the halt still stands)."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = os.path.join(
        os.path.dirname(state_path) or ".", f"scrape-{platform}-{stamp}-{time.time_ns()}.png"
    )
    try:
        browser.screenshot(path)
    except Exception:
        return None
    return path


def _collect_profile(browser, module, platform, index, record):
    handle = record["handle"]
    url = module.URL.format(handle=handle)
    options = {}
    capture_ready = getattr(module, "capture_ready", None)
    if capture_ready:
        options = {
            "ready_js": module.READY_JS,
            "capture_ready": lambda entries: capture_ready(entries, handle),
        }
    nav_result = browser.navigate(url, NAV_WAIT_MS, capture=module.CAPTURE, **options)
    captured = nav_result.get("captured", [])
    try:
        if options:
            log.info(
                "profile data wait",
                platform=platform,
                index=index,
                milliseconds=round(nav_result.get("load_ms", 0)),
                ready=nav_result.get("data_ready", False),
            )
            if not nav_result.get("dom_ready"):
                raise ExtractError("profile DOM not ready")

        ready_js = getattr(module, "READY_JS", None)
        if not options and ready_js and not browser.wait_for(ready_js, READY_TIMEOUT_S):
            log.warning("page never became ready", platform=platform, index=index, reason="timeout")

        enrich = getattr(module, "enrich", None)
        if enrich is not None:
            captured = list(captured)
            captured.extend(enrich(browser, handle) or [])

        page_text = browser.eval(PAGE_TEXT_JS) or ""
        marker = challenge_marker(page_text)
        if marker:
            raise ScrapeStopped(f"challenge page: {marker}")
        raw_eval = browser.eval(module.EXTRACTOR_JS)
    except Exception:
        # A readiness/enrichment/extractor failure must not discard responses
        # already returned by navigation. Do not capture additional page state.
        if captured:
            photos.archive_profile(platform, record["id"], None, captured)
        raise

    raw_key = photos.archive_profile(platform, record["id"], raw_eval, captured)
    eval_result = json.loads(raw_eval) if isinstance(raw_eval, str) else raw_eval
    try:
        profile = module.parse(eval_result, captured)
    except ExtractError as e:
        e.raw_r2_key = raw_key
        raise
    profile.record_id = record["id"]
    return profile, raw_key


def _store_profile(browser, platform, index, record, profile, raw_key):
    record_key = _record_key(record["id"])

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


def scrape(
    platform: str,
    max_n: int | None = None,
    state_path: str = DEFAULT_STATE_PATH,
    endpoint: str | None = None,
    data_dir: str | None = None,
    approve_command: str | None = None,
    targets: list[str] | None = None,
) -> dict:
    os.makedirs(os.path.dirname(state_path) or ".", exist_ok=True)
    # Import first: reject invalid platform names before using one in a path.
    import_module(f"people_sync.scrape.{platform}")
    with open(f"{state_path}.{platform}.run.lock", "a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"done": 0, "skipped": 0, "halted": "platform run already active"}
        return _scrape(platform, max_n, state_path, endpoint, data_dir, approve_command, targets)


def _scrape(
    platform: str,
    max_n: int | None = None,
    state_path: str = DEFAULT_STATE_PATH,
    endpoint: str | None = None,
    data_dir: str | None = None,
    approve_command: str | None = None,
    targets: list[str] | None = None,
) -> dict:
    if targets is not None and (
        not 1 <= len(targets) <= 10
        or any(not t.strip() for t in targets)
        or len(set(targets)) != len(targets)
    ):
        raise ValueError("targets must contain one to ten distinct, nonempty target IDs")
    module = import_module(f"people_sync.scrape.{platform}")
    pacer = Pacer(platform, state_path=state_path)
    records = _select_records(platform)
    if max_n is not None:
        records = records[:max_n]

    if targets:
        return _coordinated(
            module,
            platform,
            records,
            pacer,
            targets,
            endpoint,
            data_dir,
            approve_command,
            state_path,
        )

    done = 0
    skipped = 0
    halted: str | None = None
    browser = Browser.connect(endpoint=endpoint, data_dir=data_dir, approve_command=approve_command)
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
                profile, raw_key = _collect_profile(browser, module, platform, index, record)
                _store_profile(browser, platform, index, record, profile, raw_key)
            except (ScrapeStopped, photos.ArchiveError) as e:
                halted = str(e) or "run paused"
                shot = _halt_screenshot(browser, platform, state_path)
                log.warning(
                    "scrape halted", platform=platform, index=index, reason=halted, screenshot=shot
                )
                break
            except _BROWSER_LOST:
                halted = "browser lost"
                log.error("scrape halted", platform=platform, index=index, reason=halted)
                break
            except ExtractError as e:
                if str(e) == UNAVAILABLE:
                    # a deleted/renamed account: remember that so the record is
                    # not re-fetched every pass (it comes back when stale)
                    upsert_profile(
                        Profile(
                            record_id=record["id"], platform=platform, raw={"unavailable": True}
                        ),
                        raw_r2_key=e.raw_r2_key,
                    )
                log.warning("record failed", platform=platform, index=index, reason=str(e))
                skipped += 1
                time.sleep(pacer.next_gap())
                continue
            except Exception as e:
                log.warning(
                    "record failed", platform=platform, index=index, reason=type(e).__name__
                )
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


def _coordinated(
    module, platform, records, pacer, targets, endpoint, data_dir, approve_command, state_path
):
    """One bounded queue, caller-owned tabs, serial persistence, shared stop.

    The coordinator owns pacing and the budget. Workers never choose records
    or retry. File locking prevents another invocation duplicating this queue.
    """
    result = {"done": 0, "skipped": 0, "halted": None}
    stop = threading.Event()
    halt_lock = threading.Lock()
    write_lock = threading.Lock()
    browsers = []

    def halt(reason):
        with halt_lock:
            if not stop.is_set():
                result["halted"] = reason
                stop.set()
                log.warning("scrape halted", platform=platform, reason=reason)

    def process(browser, index, record):
        try:
            if stop.is_set():
                return None
            profile, raw_key = _collect_profile(browser, module, platform, index, record)
            with write_lock:
                if stop.is_set():
                    return None
                _store_profile(browser, platform, index, record, profile, raw_key)
            pacer.record()
            log.info("profile scraped", platform=platform, index=index)
            return "done"
        except (ScrapeStopped, photos.ArchiveError) as e:
            if str(e):
                halt(str(e))
            return None
        except _BROWSER_LOST:
            halt("browser lost")
            return None
        except httpx.HTTPStatusError as e:
            halt(f"HTTP {e.response.status_code} during storage or photo fetch")
            return "skipped"
        except ExtractError as e:
            if str(e) == UNAVAILABLE:
                with write_lock:
                    if not stop.is_set():
                        upsert_profile(
                            Profile(
                                record_id=record["id"], platform=platform, raw={"unavailable": True}
                            ),
                            raw_r2_key=e.raw_r2_key,
                        )
            log.warning("record failed", platform=platform, index=index, reason=str(e))
            return "skipped"
        except Exception as e:
            # With parallel work, an unknown failure must not turn into a burst
            # of repeated failed storage or source requests.
            halt(type(e).__name__)
            return "skipped"

    try:
        for target in targets:
            browser = Browser.connect(
                endpoint=endpoint,
                data_dir=data_dir,
                approve_command=approve_command,
                target_id=target,
            )
            browsers.append(browser)
            browser.watch_blocks(module.URL.format(handle=""), halt, stop)
        queue = iter(enumerate(records))
        available = list(browsers)
        pending = {}
        exhausted = False
        next_start = 0
        with ThreadPoolExecutor(max_workers=len(browsers)) as pool:
            while pending or (not exhausted and not stop.is_set()):
                if (
                    not stop.is_set()
                    and not exhausted
                    and available
                    and time.monotonic() >= next_start
                ):
                    entry = next(queue, None)
                    if entry is None:
                        exhausted = True
                    else:
                        index, record = entry
                        if not record.get("handle"):
                            result["skipped"] += 1
                            continue
                        if not pacer.reserve():
                            # The limit stops new dispatches; already-reserved
                            # profiles can still finish and be preserved.
                            with halt_lock:
                                if not stop.is_set():
                                    result["halted"] = "daily cap reached"
                            exhausted = True
                        else:
                            browser = available.pop(0)
                            pending[pool.submit(process, browser, index, record)] = browser
                            next_start = time.monotonic() + pacer.next_gap(workers=len(browsers))
                            if index == len(records) - 1:
                                exhausted = True
                            elif not pacer.allow():
                                with halt_lock:
                                    if not stop.is_set():
                                        result["halted"] = "daily cap reached"
                                exhausted = True
                if pending:
                    finished, _ = wait(pending, timeout=0.1, return_when=FIRST_COMPLETED)
                    for future in finished:
                        available.append(pending.pop(future))
                        outcome = future.result()
                        if outcome:
                            result[outcome] += 1
                elif not exhausted:
                    stop.wait(min(0.1, max(0, next_start - time.monotonic())))
    except (Exception, KeyboardInterrupt) as e:
        halt(type(e).__name__)
    finally:
        if stop.is_set():
            for browser in browsers:
                _halt_screenshot(browser, platform, state_path)
        for browser in browsers:
            browser.close()
    return result
