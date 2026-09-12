"""Partiful: the mutuals list (people sharing events with the operator) and
the profile pages behind it.

https://partiful.com/mutuals renders every mutual as a row (name, last
seen, shared-event count, thumbnail) whose click routes to /u/<uid>; the
uid is not in the markup, so `harvest` clicks each row, reads the profile
that renders (name, Instagram links, picture, event count), and goes back.
`scrape partiful` re-visits /u/<uid> directly with the same extractor.

A mutual is matched to a person by the Instagram handle on their profile
(match.py), never by name alone.
"""

import json
import random
import time

from people_sync import photos
from people_sync.scrape import snapshot
from people_sync.scrape.profile import ExtractError, Profile

URL = "https://partiful.com/u/{handle}"
LIST_URL = "https://partiful.com/mutuals"
CAPTURE: list[str] = []
ROW_SELECTOR = "[class^=mutuals_row]"
ONBOARDING_DISMISS = "text[button]=Sounds good"
ROW_PAUSE_S = (2.0, 5.0)

# The profile page: title/h1 = name, the profile picture is the imgix
# profileImages asset, Instagram links are the person's (the footer carries
# Partiful's own @partiful, excluded), event names/times list past events.
EXTRACTOR_JS = (
    "(function(){var h1=document.querySelector('h1');var name=h1?h1.innerText.trim():null;"
    "if(!name)return JSON.stringify({error:'no-profile',title:document.title});"
    "var ig=[].slice.call(document.querySelectorAll('a[href*=\"instagram.com/\"]'))"
    ".map(function(a){var m=a.href.match(/instagram\\.com\\/([A-Za-z0-9._]+)/);return m?m[1]:null})"
    ".filter(function(h){return h&&h.toLowerCase()!=='partiful'});"
    "var imgs=[].slice.call(document.querySelectorAll('img'))"
    ".filter(function(i){return /profileImages\\//.test(i.src)&&i.naturalWidth>=80})"
    ".sort(function(a,b){return b.naturalWidth-a.naturalWidth});"
    "var t=document.body.innerText.split('\\n').map(function(s){return s.trim()}).filter(Boolean);"
    "var events=t.filter(function(x,i){return /^(In about |In \\d+ |\\d+ (days?|months?|years?) ago$|Yesterday|Today)/.test(t[i+1]||'')}).length;"
    "var bday=t.filter(function(x){return /\\b(January|February|March|April|May|June|July|August|September|October|November|December) birthday$/i.test(x)})[0]||null;"
    "return JSON.stringify({name:name,instagram:ig,avatar:imgs[0]?imgs[0].src:null,"
    "events:events,birthday_month:bday,path:location.pathname});})()"
)

_MONTHS = [
    "january",
    "february",
    "march",
    "april",
    "may",
    "june",
    "july",
    "august",
    "september",
    "october",
    "november",
    "december",
]


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error"):
        raise ExtractError(eval_result["error"])
    path = (eval_result.get("path") or "").strip("/")
    uid = path.split("/", 1)[1] if path.startswith("u/") else (path or None)
    handles = [h for h in (eval_result.get("instagram") or []) if h]
    bday = eval_result.get("birthday_month")
    birthday = None
    if bday:
        month = bday.split()[0].lower()
        if month in _MONTHS:
            birthday = f"--{_MONTHS.index(month) + 1:02d}"
    return Profile(
        platform="partiful",
        profile_url=URL.format(handle=uid) if uid else "",
        platform_id=uid,
        display_name=eval_result.get("name"),
        bio=None,
        location=None,
        hometown=None,
        education=None,
        work=None,
        birthday=birthday,
        links=[f"https://www.instagram.com/{h}/" for h in handles] or None,
        is_private=None,
        is_verified=None,
        follower_count=None,
        following_count=None,
        mutual_count=eval_result.get("events"),
        avatar_url=eval_result.get("avatar"),
        raw={"extractor": eval_result, "instagram_handles": handles},
    )


ROW_JS = (
    "(function(i){var r=document.querySelectorAll(" + json.dumps(ROW_SELECTOR) + ")[i];"
    "if(!r)return null;r.id=r.id||('ps_row_'+i);r.scrollIntoView({block:'center'});"
    "var q=function(s){var e=r.querySelector(s);return e?e.innerText.trim():null};"
    "var img=r.querySelector('img');"
    "return {selector:'#'+r.id,name:q('[class^=mutuals_name]'),last_seen:q('[class^=mutuals_metadata]'),"
    "shared_events:parseInt(q('[class^=mutuals_count]')||'')||null,thumb:img?img.src.split('?')[0]:null};})(%d)"
)
ROW_COUNT_JS = "document.querySelectorAll(" + json.dumps(ROW_SELECTOR) + ").length"


def harvest(browser, start: int = 0, limit: int | None = None, pause_s=ROW_PAUSE_S):
    """Yield one entry per mutual row: the row's own fields plus the parsed
    profile (or an `error`), by clicking through and back."""
    browser.navigate(LIST_URL, 12000)
    time.sleep(4)
    if browser.eval("!!document.querySelector('[role=dialog]')"):
        browser.click(ONBOARDING_DISMISS)
        time.sleep(2)
    total = int(browser.eval(ROW_COUNT_JS) or 0)
    end = total if limit is None else min(total, start + limit)
    ordinal = 0
    for i in range(start, end):
        try:
            row = browser.eval(ROW_JS % i)
        except Exception:
            snapshot.retain_list(
                "partiful",
                [],
                ordinal=ordinal,
                scope="mutuals",
                expected_total=total,
                reason="acquisition-failed",
            )
            raise ExtractError("list-acquisition-failed") from None
        if not row:
            snapshot.retain_list(
                "partiful",
                [],
                ordinal=ordinal,
                scope="mutuals",
                expected_total=total,
                reason="row-missing",
            )
            return
        page, list_key = snapshot.retain_list(
            "partiful", [row], ordinal=ordinal, scope="mutuals", expected_total=total, entry_start=i
        )
        ordinal += 1
        refs = [snapshot.list_ref(page, list_key, 0)]
        try:
            browser.click(row["selector"])
        except Exception:
            snapshot.retain_list(
                "partiful",
                [],
                ordinal=ordinal,
                scope="mutuals",
                expected_total=total,
                reason="acquisition-failed",
            )
            raise ExtractError("list-acquisition-failed") from None
        row = page["entries"][0]
        entry = dict(row)
        if browser.wait_for("location.pathname.startsWith('/u/')", 10):
            browser.wait_for("!!document.querySelector('h1')", 8)
            time.sleep(1.0)
            entry["uid"] = browser.eval("location.pathname").rsplit("/", 1)[-1]
            context = {**row, "source_dom": snapshot.collect(browser, "partiful")}
            try:
                raw = browser.eval(EXTRACTOR_JS)
            except Exception:
                photos.archive_profile(
                    "partiful",
                    f"partiful:{entry['uid']}",
                    None,
                    [],
                    context=context | {"failure": "extraction-failed"},
                )
                raise
            payload = snapshot.prepare(
                "partiful", f"partiful:{entry['uid']}", raw, [], context=context
            )
            entry["raw_r2_key"] = photos.archive_profile(
                "partiful",
                f"partiful:{entry['uid']}",
                payload.get("raw_eval", payload["eval"]),
                [],
                context=payload["context"],
            )
            entry = {
                "uid": entry["uid"],
                "raw_r2_key": entry["raw_r2_key"],
                **{k: v for k, v in payload["context"].items() if k in snapshot._ROW},
            }
            try:
                avatar_url = snapshot.avatar_url("partiful", raw, [], entry["uid"])
                raw = payload["eval"]
                entry["profile"] = parse(json.loads(raw) if isinstance(raw, str) else raw)
                entry["_avatar_url"] = avatar_url
            except ExtractError as e:
                entry["error"] = str(e)
        else:
            entry["error"] = "no-navigation"
        entry["capture_refs"] = refs
        browser.eval("history.back()")
        browser.wait_for("location.pathname==='/mutuals' && " + ROW_COUNT_JS + ">0", 10)
        time.sleep(random.uniform(*pause_s))
        yield i, total, entry
    snapshot.retain_list(
        "partiful",
        [],
        ordinal=ordinal,
        scope="mutuals",
        expected_total=total,
        reason="row-limit" if start or end < total else "rendered-rows-exhausted",
    )


def ingest_entry(entry: dict, browser=None, index: int = 0) -> str | None:
    """Ledger + profile rows for one harvested mutual. Returns the record id,
    or None when the row never reached a profile."""
    from people_sync import ledger
    from people_sync.scrape import run as scrape_run
    from people_sync.scrape.profile import upsert_profile

    uid = entry.get("uid")
    profile = entry.get("profile")
    avatar_url = entry.pop("_avatar_url", None)
    capture_refs = tuple(entry.get("capture_refs", ()))
    if not uid or profile is None:
        return None
    # Compatibility callers also pass through the same retained-input boundary.
    if not entry.get("raw_r2_key"):
        avatar_url = snapshot.avatar_url("partiful", profile.raw["extractor"], [], uid)
        payload = snapshot.prepare(
            "partiful",
            f"partiful:{uid}",
            profile.raw["extractor"],
            [],
            context={k: v for k, v in entry.items() if k in snapshot._ROW},
        )
        raw_key = photos.archive_profile(
            "partiful", f"partiful:{uid}", payload["eval"], [], context=payload["context"]
        )
        profile = parse(payload["eval"], payload["captured"])
        entry = {
            "uid": uid,
            "raw_r2_key": raw_key,
            **{k: v for k, v in payload["context"].items() if k in snapshot._ROW},
        }
    raw = {
        "url": URL.format(handle=uid),
        "last_seen": entry.get("last_seen"),
        "shared_events": entry.get("shared_events"),
        "thumb": entry.get("thumb"),
        "instagram_handles": profile.raw.get("instagram_handles") or [],
    }
    record = ledger.Record(
        source="partiful",
        source_id=uid,
        handle=uid,
        name=entry.get("name") or profile.display_name,
        raw=raw,
        capture_key=entry["raw_r2_key"],
        capture_refs=capture_refs,
    )
    raw_key = entry["raw_r2_key"]
    ledger.upsert([record])
    profile.record_id = record.row_id
    key, sha = (None, None)
    if browser is not None and (avatar_url or profile.avatar_url):
        key, sha = scrape_run._resolve_avatar(
            browser,
            "partiful",
            index,
            scrape_run._record_key(record.row_id),
            avatar_url or profile.avatar_url,
            None,
            None,
        )
    upsert_profile(profile, avatar_r2_key=key, avatar_sha256=sha, raw_r2_key=raw_key)
    return record.row_id
