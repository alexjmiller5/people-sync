"""Facebook: the friends list (to give the export's name-only records a
profile handle) and the profile page extractor.

The friends export carries names and dates only, so `list_friends` scrolls
https://www.facebook.com/me/friends (virtualized: entries render as the
page scrolls) and collects handle, name, and mutual-friend count per entry;
`assign_handles` maps those onto ledger records by exact normalized name,
only when the name is unique on both sides.

A profile page (2026-09 layout) renders "Personal details" - "Lives in ...",
"From ...", a birthday line, gender - then Education / Work blocks, plus
"N friends • M mutual" and a 168px svg <image> avatar in the top card.
"""

import re

from people_sync.match import normalize
from people_sync.scrape.profile import ExtractError, Profile

URL = "https://www.facebook.com/{handle}"
LIST_URL = "https://www.facebook.com/me/friends"
READY_JS = (
    "/Personal details|mutual friends?|Add friend|Friends$|isn't available|No posts available/"
    ".test(document.body.innerText)"
)

CAPTURE: list[str] = []

# Profile handles are either a vanity slug or profile.php?id=<n>; both are
# kept verbatim as the handle so URL.format() reproduces the page.
_LINK_RE = (
    r"facebook\.com\/(?!friends|me$|reel|marketplace|groups|watch|gaming|events|"
    r"bookmarks|messages|notifications|settings|stories|profile\.php\?id=\d+&)"
    r"([A-Za-z0-9.]+)\/?(\?|$)|facebook\.com\/(profile\.php\?id=\d+)"
)

LIST_ENTRIES_JS = (
    "(function(){var v=function(e){var r=e.getBoundingClientRect();return r.width>0&&r.height>0};"
    "var re=/" + _LINK_RE + "/;var seen={};var out=[];"
    'var links=[].slice.call(document.querySelectorAll("a[href]")).filter(v);'
    "for(var k=0;k<links.length;k++){var a=links[k];var m=a.href.match(re);if(!m)continue;"
    "var handle=m[1]||m[3];if(!handle||seen[handle])continue;"
    'var card=a.closest("[role=listitem]")||(a.parentElement&&a.parentElement.parentElement&&a.parentElement.parentElement.parentElement);'
    'var t=(card?card.innerText:"").split("\\n").map(function(s){return s.trim()}).filter(Boolean);'
    "if(!t.length)continue;seen[handle]=1;"
    "var mut=t.filter(function(x){return /mutual friends?$/.test(x)})[0]||null;"
    "out.push({handle:handle,name:t[0],mutual_text:mut});}"
    "return JSON.stringify(out);})()"
)

LIST_LINK_COUNT_JS = 'document.querySelectorAll("a[href]").length'

EXTRACTOR_JS = (
    "(function(){if(/This content isn't available right now|This page isn't available/i.test(document.body.innerText))"
    'return JSON.stringify({error:"unavailable"});'
    'var main=document.querySelector("[role=main]")||document.body;'
    'var t=main.innerText.split("\\n").map(function(s){return s.trim()}).filter(Boolean);'
    'if(!t.length)return JSON.stringify({error:"no-main",title:document.title});'
    'var name=[].slice.call(document.querySelectorAll("h1")).map(function(e){return e.innerText.trim()})'
    ".filter(function(x){return x&&!/^(Notifications|Facebook)$/.test(x)})[0]||null;"
    "var f=function(re){return t.filter(function(x){return re.test(x)})};"
    "var first=function(re){return f(re)[0]||null};"
    "var counts=first(/\\bfriends\\b.*mutual|^[\\d,.K]+ friends$/);"
    "var section=function(label,stops){var i=t.indexOf(label);if(i<0)return [];var out=[];"
    "for(var k=i+1;k<t.length&&k<i+12;k++){if(stops.test(t[k]))break;out.push(t[k])}return out};"
    'var personal=section("Personal details",/^(Education|Work|Places lived|Contact info|Basic info|See more|Family|Relationship)/);'
    'var education=section("Education",/^(Work|Places lived|Contact info|Basic info|See more|Family|Personal details|Relationship|Photos)/);'
    'var work=section("Work",/^(Education|Places lived|Contact info|Basic info|See more|Family|Personal details|Relationship|Photos)/);'
    'var imgs=[].slice.call(document.querySelectorAll("image, img")).filter(function(e){var r=e.getBoundingClientRect();return r.width>=120&&r.width<=220&&r.top<800});'
    'var img=imgs[0]?(imgs[0].getAttribute("xlink:href")||imgs[0].getAttribute("href")||imgs[0].src):null;'
    "var priv=/This content isn't available|No posts available|Only friends can see/i.test(main.innerText);"
    "return JSON.stringify({name:name,counts:counts,personal:personal,education:education,work:work,"
    "lives_in:first(/^Lives in /),from:first(/^From /),avatar:img,restricted:priv,"
    "path:window.location.pathname,search:window.location.search});})()"
)

_MUTUAL = re.compile(r"([\d,]+)\s+mutual")
_FRIENDS = re.compile(r"([\d,.]+[Kk]?)\s+friends")
_MONTHS = [
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
]
_DATE_LONG = re.compile(r"^(" + "|".join(_MONTHS) + r") (\d{1,2}), (\d{4})$")
_DATE_SHORT = re.compile(r"^(" + "|".join(_MONTHS) + r") (\d{1,2})$")


def _int(s: str | None) -> int | None:
    if not s:
        return None
    s = s.replace(",", "")
    if s[-1] in "Kk":
        return int(float(s[:-1]) * 1000)
    return int(float(s))


def _birthday(personal: list[str]) -> str | None:
    """ "December 9, 2002" -> 2002-12-09; a month-day only line -> "--12-09"
    (the year is hidden); nothing else is a birthday."""
    for line in personal:
        m = _DATE_LONG.match(line)
        if m:
            return f"{m.group(3)}-{_MONTHS.index(m.group(1)) + 1:02d}-{int(m.group(2)):02d}"
        m = _DATE_SHORT.match(line)
        if m:
            return f"--{_MONTHS.index(m.group(1)) + 1:02d}-{int(m.group(2)):02d}"
    return None


def _strip(prefix: str, line: str | None) -> str | None:
    return line[len(prefix) :].strip() if line and line.startswith(prefix) else None


def _entries(block: list[str]) -> list[str]:
    """Education/Work render as name lines with detail lines ("Class of
    2025", "Went to ...", dates) beneath; keep the institution lines."""
    keep = []
    for line in block:
        if re.match(
            r"^(Class of \d{4}|Went to |Studied |Studies |Works at |Worked at |Past:|Current:|\d{4}\b|See more)",
            line,
        ):
            continue
        if line and line not in keep:
            keep.append(line)
    return keep


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error"):
        raise ExtractError(eval_result["error"])
    path = (eval_result.get("path") or "").strip("/")
    if path == "profile.php":
        m = re.search(r"id=(\d+)", eval_result.get("search") or "")
        handle = f"profile.php?id={m.group(1)}" if m else None
    else:
        handle = path or None
    counts = eval_result.get("counts") or ""
    mutual = _MUTUAL.search(counts)
    friends = _FRIENDS.search(counts)
    personal = list(eval_result.get("personal") or [])
    education = _entries(list(eval_result.get("education") or []))
    work = _entries(list(eval_result.get("work") or []))
    return Profile(
        platform="facebook",
        profile_url=URL.format(handle=handle) if handle else "",
        platform_id=handle,
        display_name=eval_result.get("name"),
        bio=None,
        location=_strip("Lives in ", eval_result.get("lives_in")),
        hometown=_strip("From ", eval_result.get("from")),
        education=education or None,
        work=work or None,
        birthday=_birthday(personal),
        links=None,
        is_private=eval_result.get("restricted") or None,
        is_verified=None,
        follower_count=_int(friends.group(1)) if friends else None,
        following_count=None,
        mutual_count=int(mutual.group(1).replace(",", "")) if mutual else None,
        avatar_url=eval_result.get("avatar"),
        raw={"extractor": eval_result},
    )


def assign_handles(entries: list[dict], records: list[dict]) -> list[dict]:
    """Which ledger records (id, name, handle) get which handle: exact
    normalized-name matches only, and only when that name occurs once in
    the friends list AND once among the records; records that already
    carry a handle are left alone. Returns [{id, handle}]."""
    by_name: dict[str, list[dict]] = {}
    for e in entries:
        by_name.setdefault(normalize(e.get("name") or ""), []).append(e)
    rec_by_name: dict[str, list[dict]] = {}
    for r in records:
        rec_by_name.setdefault(normalize(r.get("name") or ""), []).append(r)
    out = []
    for name, es in by_name.items():
        rs = rec_by_name.get(name) or []
        if not name or len(es) != 1 or len(rs) != 1 or rs[0].get("handle"):
            continue
        out.append({"id": rs[0]["id"], "handle": es[0]["handle"]})
    return out


def list_friends(browser, max_scrolls: int = 60, settle_s: float = 2.5) -> list[dict]:
    """Scroll the friends page until the link count stops growing and return
    the entries (handle, name, mutual_text)."""
    import json
    import time

    browser.navigate(LIST_URL, 12000)
    time.sleep(settle_s)
    counts: list[int] = []
    for _ in range(max_scrolls):
        browser.scroll(2000)
        time.sleep(settle_s)
        counts.append(int(browser.eval(LIST_LINK_COUNT_JS) or 0))
        if len(counts) >= 3 and counts[-1] == counts[-2] == counts[-3]:
            break
    raw = browser.eval(LIST_ENTRIES_JS)
    return json.loads(raw) if isinstance(raw, str) else (raw or [])
