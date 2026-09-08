"""LinkedIn profile extractor (recon-linkedin.md, re-validated live 2026-09-08).

The page is a client-rendered shell; the top card of `main` carries what
triage needs - name, pronouns, headline, location, the current company and
school listed after "Contact info", mutual-connection text, connection and
follower counts, the "You both ..." highlights, and the About text - and the
profile picture is the `profile-displayphoto` image. Experience and
education sections are not in the rendered text, and the Voyager GraphQL
calls that carry them do not fire on every load, so any captured response
is kept verbatim in `raw` for later parsing rather than relied on.
"""

import re

from people_sync.scrape.profile import ExtractError, Profile

URL = "https://www.linkedin.com/in/{handle}/"

CAPTURE = [r"voyager/api/graphql"]

EXTRACTOR_JS = (
    '(function(){var m=document.querySelector("main");if(!m)return '
    'JSON.stringify({error:"no-main",title:document.title});'
    'var t=m.innerText.split("\\n").map(s=>s.trim()).filter(Boolean);'
    'if(!t.length)return JSON.stringify({error:"no-main",title:document.title});'
    "var head=t.slice(0,30);"
    "var pron=/^(he|she|they|xe|ze)\\s*\\/\\s*\\w+/i;"
    "var deg=/^·?\\s*(1st|2nd|3rd)(\\+)?$|· (1st|2nd|3rd)/;"
    "var name=t[0]||null;var i=1;var pronouns=null;"
    "if(t[i]&&pron.test(t[i])){pronouns=t[i];i++}"
    "if(t[i]&&deg.test(t[i]))i++;"
    "var headline=t[i]&&!deg.test(t[i])?t[i]:null;"
    'var ci=head.indexOf("Contact info");'
    "var location=null;"
    'if(ci>0){var j=ci-1;if(head[j]==="·")j--;location=head[j]||null}'
    "if(!location){location=head.find(x=>/, .*(United States|USA|UK|Canada|Spain|France|Germany|Area)$|Area$/.test(x))||null}"
    "var orgs=ci>0?head.slice(ci+1,ci+3).filter(x=>!/connections$|followers$|mutual connection/.test(x)):[];"
    "var f=re=>head.find(x=>re.test(x))||null;"
    "var mut=f(/mutual connection/);var conn=f(/connections$/);var fol=f(/followers$/);"
    "var hl=t.filter(x=>/^You both /.test(x));"
    'var ab=t.indexOf("About");'
    'var about=ab>=0?t.slice(ab+1,ab+6).filter(x=>!/^(… more|Top skills)$/.test(x)).join("\\n"):null;'
    'var img=m.querySelector("img[src*=profile-displayphoto]")||'
    'm.querySelector("img[alt*=profile i], img.pv-top-card-profile-picture__image");'
    "return JSON.stringify({name:name,pronouns:pronouns,headline:headline,location:location,"
    "orgs:orgs,mutual_text:mut,connections:conn,followers:fol,highlights:hl,about:about,"
    "avatar:img?img.src:null,path:location.pathname});})()"
)

_MUTUAL = re.compile(r"(\d+)\s+other mutual connection|(\d+)\s+mutual connection")
_COUNT = re.compile(r"([\d,]+)(\+)?\s+(connections|followers)")


def _mutual_count(text: str | None) -> int | None:
    """ "A, B and 22 other mutual connections" = 24; "A and B are mutual
    connections" = 2; "1 mutual connection" = 1."""
    if not text:
        return None
    m = _MUTUAL.search(text)
    named = len(
        [
            p
            for p in re.split(r",\s*|\s+and\s+", text.split(" are ")[0].split(" other")[0])
            if p and not p[0].isdigit()
        ]
    )
    if m and m.group(1):
        return int(m.group(1)) + named
    if m and m.group(2):
        return int(m.group(2))
    return named if " mutual connection" in text else None


def _count(text: str | None) -> int | None:
    if not text:
        return None
    m = _COUNT.search(text.replace(",", ""))
    return int(m.group(1)) if m else None


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error"):
        raise ExtractError(eval_result["error"])

    orgs = [o for o in (eval_result.get("orgs") or []) if o]
    path = (eval_result.get("path") or "").strip("/")
    platform_id = path.split("/", 1)[1] if path.startswith("in/") else (path or None)
    raw: dict = {"extractor": eval_result}
    voyager = [
        c
        for c in (captured or [])
        if c.get("body") and ("Position" in c["body"] or "Education" in c["body"])
    ]
    if voyager:
        raw["voyager"] = [{"url": c.get("url"), "body": c.get("body")} for c in voyager]

    return Profile(
        platform="linkedin",
        profile_url=URL.format(handle=platform_id) if platform_id else "",
        platform_id=platform_id,
        display_name=eval_result.get("name"),
        bio="\n".join(x for x in (eval_result.get("headline"), eval_result.get("about")) if x)
        or None,
        location=eval_result.get("location"),
        hometown=None,
        education=orgs[1:2] or None,
        work=orgs[0:1] or None,
        birthday=None,
        links=None,
        is_private=None,
        is_verified=None,
        follower_count=_count(eval_result.get("followers")),
        following_count=None,
        mutual_count=_mutual_count(eval_result.get("mutual_text")),
        avatar_url=eval_result.get("avatar"),
        raw=raw,
    )
