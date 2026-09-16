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
import re
import time

from people_sync import photos
from people_sync.scrape import snapshot
from people_sync.scrape.profile import ExtractError, Profile

URL = "https://partiful.com/u/{handle}"
LIST_URL = "https://partiful.com/mutuals"
EVENTS_URL = "https://partiful.com/events?category=all_past_events"
EVENT_URL = "https://partiful.com/e/{event_id}"
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
    "if(!r)return null;var id=r.id||('ps_row_'+i);"
    "var q=function(s){var e=r.querySelector(s);return e?e.innerText.trim():null};"
    "var img=r.querySelector('img');"
    "return {selector:'#'+id,name:q('[class^=mutuals_name]'),last_seen:q('[class^=mutuals_metadata]'),"
    "shared_events:parseInt(q('[class^=mutuals_count]')||'')||null,thumb:img?img.src.split('?')[0]:null};})(%d)"
)
ROW_PREPARE_JS = (
    "(function prepareRow(i){var rows=function(){return document.querySelectorAll("
    + json.dumps(ROW_SELECTOR)
    + ")};var r=rows()[i];if(!r)return false;"
    "var text=r.innerText;r.id=r.id||('ps_row_'+i);r.scrollIntoView({block:'center'});"
    "return rows()[i]===r&&r.innerText===text;})(%d)"
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
            if not browser.eval(ROW_PREPARE_JS % i):
                raise ExtractError("mutual-row-changed")
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


# --- events ---------------------------------------------------------------------

EVENTS_JS = (
    "(function(){var seen={};var out=[];"
    "document.querySelectorAll('a[href*=\"/e/\"]').forEach(function(a){"
    "var m=(a.getAttribute('href')||'').match(/\\/e\\/([A-Za-z0-9_-]+)/);if(!m||seen[m[1]])return;seen[m[1]]=1;"
    "var lines=(a.innerText||'').split('\\n').map(function(t){return t.trim()}).filter(Boolean);"
    "var status=null,when=null,title=null;lines.forEach(function(t){"
    "if(/WENT|HOSTING|DIDN'T GO|MAYBE|INTERESTED|CANCELED|CAN'T GO|FOLLOWING/.test(t))status=t.replace(/^[^A-Z]*/,'');"
    "else if(!when&&/\\bat\\s*\\d/.test(t))when=t;else if(!title&&!/^Hosted by/.test(t))title=t;});"
    "out.push({id:m[1],title:title,when:when,status:status});});return out;})()"
)
GUEST_ROWS_JS = (
    "(function(){var d=document.querySelector('[role=dialog]');if(!d)return null;"
    "var rows=[];var section=null;"
    "d.querySelectorAll('*').forEach(function(e){if(e.children.length)return;var t=(e.innerText||'').trim();"
    "if(!t)return;if(/^(Going|Went|Maybe|Can't Go|Invited)$/.test(t)){section=t;return;}"
    "if(/^\\d+$/.test(t)||t==='Guest List'||/^[A-Z]{1,2}$/.test(t))return;"
    "var m=t.match(/^and (\\d+) more$/);if(m){if(rows.length)rows[rows.length-1].plus_ones=parseInt(m[1]);return;}"
    "rows.push({name:t,section:section,plus_ones:0,el:e});});"
    "rows.forEach(function(r,i){r.el.id=r.el.id||('ps_guest_'+i);r.selector='#'+r.el.id;delete r.el;});return rows;})()"
)
GUEST_COUNTS_JS = (
    "(function(){var d=document.querySelector('[role=dialog]');if(!d)return null;"
    "var t=(d.innerText||'').split('\\n').map(function(s){return s.trim()}).filter(Boolean);"
    "var out={};for(var i=0;i+1<t.length;i++){if(/^(Going|Went|Maybe|Can't Go|Invited)$/.test(t[i])&&/^\\d+$/.test(t[i+1]))out[t[i]]=parseInt(t[i+1]);}return out;})()"
)
GUEST_PAUSE_S = (2.0, 4.0)


def assign_sections(rows: list[dict], counts: dict) -> list[dict]:
    """The dialog lists section names with their counts once at the top, then
    every guest in that order; a guest's section is where its cumulative
    position (counting plus-ones) falls."""
    order = [s for s in ("Going", "Went", "Maybe", "Invited", "Can't Go") if s in counts]
    if not order:
        return rows
    remaining = {s: counts[s] for s in order}
    current = 0
    out = []
    for row in rows:
        while current < len(order) - 1 and remaining[order[current]] <= 0:
            current += 1
        section = order[current]
        remaining[section] -= 1 + int(row.get("plus_ones") or 0)
        out.append({**row, "section": section})
    return out


ROLES = {"Went": "went", "Going": "went", "Maybe": "maybe", "Invited": "invited"}


def parse_event_when(text: str | None, year: int | None = None) -> str | None:
    """'Sat 9/5 at 8:30pm' -> '2026-09-05T20:30' when a year is known; else the text."""
    if not text:
        return None
    m = re.search(r"(\d{1,2})/(\d{1,2}) at (\d{1,2})(?::(\d{2}))?(am|pm)", text)
    if not m or not year:
        return text
    month, day, hour, minute, ampm = m.groups()
    hour = int(hour) % 12 + (12 if ampm == "pm" else 0)
    return f"{year:04d}-{int(month):02d}-{int(day):02d}T{hour:02d}:{int(minute or 0):02d}"


def harvest_events(browser):
    """The signed-in user's past events, retained as one list observation."""
    browser.navigate(EVENTS_URL, 12000)
    time.sleep(3)
    for _ in range(8):
        browser.eval("window.scrollTo(0, document.body.scrollHeight)")
        time.sleep(1.5)
    events = browser.eval(EVENTS_JS) or []
    # No platform-verified total exists for the events page, so the observation
    # stays "partial" like every other scrolled list.
    page, key = snapshot.retain_list(
        "partiful", events, ordinal=0, scope="events", expected_total=len(events)
    )
    return page["entries"], key


def harvest_event_guests(browser, event_id: str, pause_s=GUEST_PAUSE_S):
    """Yield (guest entry, capture ref) per guest of one event, click-walking each
    row to its /u/<uid>. Guests without a profile keep their name only."""
    browser.navigate(EVENT_URL.format(event_id=event_id), 12000)
    rendered = "!!document.title && document.body.innerText.length > 200"
    if not browser.wait_for(rendered, 20):
        browser.eval("location.reload()")  # a blank first paint under load; once
        if not browser.wait_for(rendered, 25):
            raise ExtractError("event-page-blank")
    time.sleep(2)
    header = {
        "title": browser.eval("(document.querySelector('h1')||{}).innerText||null"),
        "when": browser.eval(
            "([...document.querySelectorAll('h1 ~ *, main *')].map(e=>e.innerText||'')"
            ".find(t=>/^[A-Z][a-z]+, [A-Z][a-z]{2} \\d{1,2}, \\d{4}/.test(t.trim()))||null)"
        ),
    }
    # Client-rendered; the guest list can take well over the load event to paint,
    # longer still with several tabs loading at once.
    view_all = "[...document.querySelectorAll('*')].some(e=>!e.children.length&&(e.innerText||'').trim()==='View all')"
    if not browser.eval("!!document.querySelector('[role=dialog]')"):
        if not browser.wait_for(view_all, 25):
            raise ExtractError("guest-list-not-rendered")
        browser.eval(
            "(function(){var e=[...document.querySelectorAll('*')].find(function(x){return !x.children.length&&(x.innerText||'').trim()==='View all'});"
            "if(e)e.scrollIntoView({block:'center'});return !!e})()"
        )
        time.sleep(0.8)
        browser.click("text=View all")
        browser.wait_for("!!document.querySelector('[role=dialog]')", 10)
        time.sleep(1.5)
    rows = assign_sections(browser.eval(GUEST_ROWS_JS) or [], browser.eval(GUEST_COUNTS_JS) or {})
    ordinal = 0
    failures = 0
    for index, row in enumerate(rows):
        entry = {
            "event_id": event_id,
            "name": row.get("name"),
            "section": row.get("section"),
            "plus_ones": row.get("plus_ones") or 0,
            "uid": None,
        }
        try:
            if not browser.eval("!!document.querySelector('[role=dialog]')"):
                browser.click("text=View all")
                browser.wait_for("!!document.querySelector('[role=dialog]')", 10)
                time.sleep(1.0)
                browser.eval(GUEST_ROWS_JS)  # re-tag rows after the dialog reopened
            browser.eval(
                "(function(){var e=document.querySelector(%s);if(e)e.scrollIntoView({block:'center'});"
                "return !!e})()" % json.dumps(row["selector"])
            )
            browser.click(row["selector"])
            if browser.wait_for("location.pathname.startsWith('/u/')", 15):
                entry["uid"] = browser.eval("location.pathname").rsplit("/", 1)[-1]
                browser.eval("history.back()")
                browser.wait_for("location.pathname.startsWith('/e/')", 15)
                time.sleep(1.0)
            failures = 0
        except Exception:
            # One guest row failing (a slow paint, a row re-rendered mid-click) is
            # retained as a failed observation and the walk continues; a run of
            # three means the page is gone.
            failures += 1
            snapshot.retain_list(
                "partiful", [], ordinal=ordinal, scope="event_guests", reason="acquisition-failed"
            )
            ordinal += 1
            if failures >= 3:
                raise ExtractError("list-acquisition-failed") from None
            time.sleep(random.uniform(*pause_s))
            continue
        page, key = snapshot.retain_list(
            "partiful",
            [entry],
            ordinal=ordinal,
            scope="event_guests",
            expected_total=len(rows),
            entry_start=index,
        )
        ordinal += 1
        yield header, page["entries"][0], snapshot.list_ref(page, key, 0)
        time.sleep(random.uniform(*pause_s))


def ingest_guest(header: dict, event: dict, guest: dict, ref: dict) -> str | None:
    """Add this event to the guest's Partiful record (created if new), keeping the
    record's other raw fields. Returns the record id, or None without a uid."""
    from people_sync import ledger, lifedata

    uid = guest.get("uid")
    if not uid:
        return None
    row_id = f"partiful:{uid}"
    existing = lifedata.sql(
        f"SELECT raw, name FROM people_sync_records WHERE id = {lifedata.sq(row_id)}"
    )
    raw = {}
    if existing:
        try:
            raw = json.loads(existing[0]["raw"] or "{}")
        except json.JSONDecodeError:
            raw = {}
    raw.setdefault("url", URL.format(handle=uid))
    role = "hosted" if (event.get("status") or "").upper().startswith("HOSTING") else None
    role = role or ROLES.get(guest.get("section") or "", "invited")
    year = None
    if header.get("when"):
        m = re.search(r"(\d{4})", header["when"])
        year = int(m.group(1)) if m else None
    item = {
        "id": event["id"],
        "title": event.get("title") or header.get("title"),
        "starts_at": parse_event_when(event.get("when"), year) or header.get("when"),
        "role": role,
        "capture_key": ref["capture_key"],
    }
    events = [e for e in raw.get("events", []) if e.get("id") != event["id"]] + [item]
    raw["events"] = sorted(events, key=lambda e: str(e.get("starts_at") or ""))
    record = ledger.Record(
        "partiful",
        uid,
        uid,
        (existing[0]["name"] if existing else None) or guest.get("name"),
        raw,
        capture_refs=(ref,),
    )
    ledger.upsert([record])
    return row_id
