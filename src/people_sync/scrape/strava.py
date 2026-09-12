"""Strava: the signed-in athlete's followers and following lists (a few
people) and athlete profile pages.

/dashboard carries a link to the signed-in athlete (`/athletes/<id>`);
`/athletes/<id>/follows?type=followers|following` renders
`ul.list-athletes` entries (link, name, location, avatar). An athlete page
renders the name as its h1, a `.location`, and an avatar image.
"""

import json
import time

from people_sync.scrape.profile import ExtractError, Profile
from people_sync.scrape import snapshot

URL = "https://www.strava.com/athletes/{handle}"
DASHBOARD_URL = "https://www.strava.com/dashboard"
FOLLOWS_URL = "https://www.strava.com/athletes/{athlete_id}/follows?type={kind}"
CAPTURE: list[str] = []

ME_JS = (
    "(function(){var a=[].slice.call(document.querySelectorAll('a[href]'))"
    ".map(function(a){return a.getAttribute('href')}).filter(function(h){return /^\\/athletes\\/\\d+\\/?$/.test(h)})[0];"
    "return a?a.replace(/\\D/g,''):null;})()"
)

LIST_JS = (
    "(function(me){var out=[];"
    'if(!document.querySelector("ul.list-athletes"))return null;'
    "var as=[].slice.call(document.querySelectorAll('ul.list-athletes a[href*=\"/athletes/\"]'));"
    "for(var i=0;i<as.length;i++){var a=as[i];var h=a.getAttribute('href')||'';var m=h.match(/\\/athletes\\/(\\d+)\\/?$/);"
    "if(!m)continue;"
    "var c=a.closest('li')||a.parentElement;var t=(c.innerText||'').split('\\n').map(function(s){return s.trim()}).filter(function(s){return s&&!/^(Following|Follow|Requested)$/.test(s)});"
    "var img=c.querySelector('img');"
    "out.push({id:m[1],name:t[0]||null,location:t[1]||null,avatar:img?img.src:null});}"
    "return JSON.stringify(out);})(%s)"
)

EXTRACTOR_JS = (
    "(function(){var h1=document.querySelector('h1');var name=h1?h1.innerText.trim():null;"
    "if(!name)return JSON.stringify({error:'no-profile',title:document.title});"
    "var loc=document.querySelector('.location, [class*=location]');"
    "var img=document.querySelector('img.avatar-img, .avatar img, img[src*=\"pictures/athletes\"], img[src*=googleusercontent]');"
    "var t=document.body.innerText;var f=function(re){var m=t.match(re);return m?parseInt(m[1].replace(/,/g,'')):null};"
    "return JSON.stringify({name:name,location:loc?loc.innerText.trim():null,avatar:img?img.src:null,"
    "followers:f(/([\\d,]+)\\s*\\n?\\s*Followers/i),following:f(/([\\d,]+)\\s*\\n?\\s*Following/i),path:location.pathname});})()"
)


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error"):
        raise ExtractError(eval_result["error"])
    path = (eval_result.get("path") or "").strip("/")
    athlete_id = (
        path.split("/")[1] if path.startswith("athletes/") and len(path.split("/")) > 1 else None
    )
    return Profile(
        platform="strava",
        profile_url=URL.format(handle=athlete_id) if athlete_id else "",
        platform_id=athlete_id,
        display_name=eval_result.get("name"),
        bio=None,
        location=eval_result.get("location") or None,
        hometown=None,
        education=None,
        work=None,
        birthday=None,
        links=None,
        is_private=None,
        is_verified=None,
        follower_count=eval_result.get("followers"),
        following_count=eval_result.get("following"),
        mutual_count=None,
        avatar_url=eval_result.get("avatar"),
        raw={"extractor": eval_result},
    )


def list_athletes(browser, settle_s: float = 5.0) -> list[dict]:
    """Followers and following of the signed-in athlete, merged by id with
    follows_me / i_follow flags."""
    browser.navigate(DASHBOARD_URL, 12000)
    time.sleep(settle_s)
    me = browser.eval(ME_JS)
    if not me:
        raise ExtractError("no-athlete")
    snapshot._identity(me, "strava")
    merged: dict[str, dict] = {}
    for ordinal, (kind, flag) in enumerate(
        (("followers", "follows_me"), ("following", "i_follow"))
    ):
        try:
            browser.navigate(FOLLOWS_URL.format(athlete_id=me, kind=kind), 12000)
            time.sleep(settle_s)
            raw = browser.eval(LIST_JS % json.dumps(me))
        except Exception:
            snapshot.retain_list(
                "strava",
                [],
                ordinal=ordinal,
                scope=kind,
                account_id=me,
                reason="acquisition-failed",
            )
            raise ExtractError("list-acquisition-failed") from None
        page, key = snapshot.retain_list("strava", raw, ordinal=ordinal, scope=kind, account_id=me)
        for index, e in enumerate(page["entries"]):
            if not e.get("id") or e["id"] == me:
                continue
            entry = merged.setdefault(
                e["id"], {**e, "follows_me": None, "i_follow": None, "capture_refs": []}
            )
            entry["capture_refs"].append(snapshot.list_ref(page, key, index))
            entry[flag] = 1
    for ordinal, kind in enumerate(("followers", "following"), start=2):
        snapshot.retain_list(
            "strava", [], ordinal=ordinal, scope=kind, account_id=me, reason="coverage-unverified"
        )
    return list(merged.values())


def ingest_entries(entries: list[dict]) -> dict:
    from people_sync import ledger

    records = [
        ledger.Record(
            source="strava",
            source_id=e["id"],
            handle=e["id"],
            name=e.get("name"),
            raw={
                "url": URL.format(handle=e["id"]),
                "location": e.get("location"),
                "avatar": e.get("avatar"),
            },
            follows_me=e.get("follows_me"),
            i_follow=e.get("i_follow"),
            capture_refs=tuple(e.get("capture_refs", ())),
        )
        for e in entries
    ]
    return ledger.upsert(records)
