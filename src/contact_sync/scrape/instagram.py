"""Instagram profile extractor (recon-instagram.md, validated 2026-09-04).

Data comes from two places: the rendered `<header>` (always present, even for
private accounts) and, when the page fires it, the `web_profile_info` XHR -
whose structured fields (verified flag, edge counts, hi-res avatar) are more
reliable than the header scrape and are preferred when captured.
"""

import json

from contact_sync.scrape.profile import Profile

URL = "https://www.instagram.com/{handle}/"

# Matched with re.search against response URLs by cdp.Browser.navigate().
CAPTURE = [r"web_profile_info"]

EXTRACTOR_JS = (
    '(function(){var h=document.querySelector("header");if(!h)return '
    'JSON.stringify({error:"no-header",title:document.title});'
    'var t=h.innerText.split("\\n").map(s=>s.trim()).filter(Boolean);'
    'var img=h.querySelector("img");'
    'var links=[...h.querySelectorAll("a[href]")].map(a=>a.href);'
    'var num=s=>{var m=(s||"").replace(/,/g,"").match(/([\\d.]+)([KkMm]?)/);'
    "if(!m)return null;var v=parseFloat(m[1]);"
    'return Math.round(m[2].toLowerCase()==="k"?v*1e3:m[2].toLowerCase()==="m"?v*1e6:v)};'
    "var f=re=>t.find(x=>re.test(x));"
    "var fb=f(/^Followed by/);var mut=null;"
    "if(fb){var m=fb.match(/\\+ (\\d+) more/);"
    'var names=(fb.replace(/^Followed by /,"").replace(/ \\+ \\d+ more$/,"")).split(/,\\s*|\\s+and\\s+/).filter(Boolean);'
    "mut=names.length+(m?parseInt(m[1]):0)}"
    "return JSON.stringify({"
    'username:location.pathname.replace(/\\//g,""),'
    "full_name:t[1]||null,"
    "pronouns:f(/^(he|she|they|xe|ze)\\b/i)||null,"
    "posts:num(f(/posts?$/)),"
    "followers:num(f(/followers$/)),"
    "following:num(f(/following$/)),"
    "bio_lines:t.filter(x=>!/^(\\d[\\d,.KkMm]* (posts?|followers|following))$|^Followed by|"
    "^(Follow|Following|Message|Follow Back|Requested)$/.test(x)).slice(2,12),"
    "private:/This account is private/i.test(document.body.innerText),"
    "verified:!!h.querySelector('svg[aria-label=\"Verified\"]'),"
    "mutual_count:mut,"
    "mutual_text:fb||null,"
    "avatar:img?img.src:null,"
    "links:links.filter(u=>!/instagram\\.com\\/(explore|direct|accounts|p\\/|reel\\/)/.test(u)&&"
    "!u.endsWith(location.pathname))});})()"
)


def _web_profile_info(captured: list[dict] | None) -> dict | None:
    for entry in captured or []:
        if "web_profile_info" not in (entry.get("url") or ""):
            continue
        try:
            body = json.loads(entry.get("body") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        if ((body or {}).get("data") or {}).get("user"):
            return body
    return None


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    bio_lines = list(eval_result.get("bio_lines") or [])
    pronouns = eval_result.get("pronouns")
    if pronouns:
        bio_lines = [line for line in bio_lines if line != pronouns]
    bio = "\n".join(bio_lines) if bio_lines else None

    username = eval_result.get("username")
    is_verified = eval_result.get("verified")
    follower_count = eval_result.get("followers")
    following_count = eval_result.get("following")
    avatar_url = eval_result.get("avatar")

    raw: dict = {"extractor": eval_result}
    if pronouns:
        raw["pronouns"] = pronouns

    body = _web_profile_info(captured)
    if body is not None:
        raw["web_profile_info"] = body
        user = body["data"]["user"]
        if user.get("is_verified") is not None:
            is_verified = user["is_verified"]
        if (user.get("edge_followed_by") or {}).get("count") is not None:
            follower_count = user["edge_followed_by"]["count"]
        if (user.get("edge_follow") or {}).get("count") is not None:
            following_count = user["edge_follow"]["count"]
        if user.get("profile_pic_url_hd"):
            avatar_url = user["profile_pic_url_hd"]

    return Profile(
        platform="instagram",
        profile_url=URL.format(handle=username) if username else "",
        platform_id=username,
        display_name=eval_result.get("full_name"),
        bio=bio,
        location=None,
        hometown=None,
        education=None,
        work=None,
        birthday=None,
        links=list(eval_result.get("links") or []) or None,
        is_private=eval_result.get("private"),
        is_verified=is_verified,
        follower_count=follower_count,
        following_count=following_count,
        mutual_count=eval_result.get("mutual_count"),
        avatar_url=avatar_url,
        raw=raw,
    )
