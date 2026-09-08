"""Instagram profile extractor (recon-instagram.md, validated 2026-09-04).

Data comes from two places: the rendered `<header>` (always present, even for
private accounts) and, when the page fires it, the `web_profile_info` XHR -
whose structured fields (verified flag, edge counts, hi-res avatar) are more
reliable than the header scrape and are preferred when captured.
"""

import json
import re
from urllib.parse import unquote

from people_sync.scrape.profile import ExtractError, Profile

URL = "https://www.instagram.com/{handle}/"

# Matched with re.search against response URLs by cdp.Browser.navigate().
# The profile JSON rides on `web_profile_info` on some loads and on a
# `graphql/query` response (the user object nested under the feed timeline)
# on others - both are captured and searched for the handle's user object.
CAPTURE = [r"web_profile_info", r"graphql/query"]

EXTRACTOR_JS = (
    "(function(){if(/Sorry, this page isn't available/i.test(document.body.innerText))"
    'return JSON.stringify({error:"unavailable"});'
    'var h=document.querySelector("header");if(!h)return '
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
    "bio_lines:(function(){var l=t.filter(x=>!/^(\\d[\\d,.KkMm]* (posts?|followers|following))$|^Followed by|"
    "^(Follow|Following|Message|Follow Back|Requested)$/.test(x)).slice(2,12);"
    'var h=l.indexOf("Highlights");return h>=0?l.slice(0,h):l})(),'
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


def _find_user(node, username: str, depth: int = 0) -> dict | None:
    """The first dict anywhere in `node` whose `username` is `username` and
    that carries profile fields (the feed-timeline response nests it)."""
    if depth > 12:
        return None
    if isinstance(node, dict):
        if node.get("username") == username and any(
            k in node
            for k in (
                "profile_pic_url_hd",
                "hd_profile_pic_url_info",
                "is_private",
                "follower_count",
            )
        ):
            return node
        for v in node.values():
            found = _find_user(v, username, depth + 1)
            if found:
                return found
    elif isinstance(node, list):
        for v in node[:50]:
            found = _find_user(v, username, depth + 1)
            if found:
                return found
    return None


def _graphql_user(captured: list[dict] | None, username: str | None) -> dict | None:
    if not username:
        return None
    for entry in captured or []:
        if "graphql/query" not in (entry.get("url") or ""):
            continue
        try:
            body = json.loads(entry.get("body") or "")
        except (json.JSONDecodeError, TypeError):
            continue
        user = _find_user(body, username)
        if user:
            return user
    return None


_JUNK_LINK = re.compile(
    r"instagram\.com/(explore|direct|accounts|p/|reel/|stories/)|/followers/|/following/|#$"
)


def _clean_links(links: list[str], username: str | None) -> list[str]:
    out: list[str] = []
    for u in links or []:
        if _JUNK_LINK.search(u) or (
            username and u.rstrip("/").endswith(f"instagram.com/{username}")
        ):
            continue
        m = re.match(r"https://l\.instagram\.com/\?u=([^&]+)", u)
        if m:
            u = unquote(m.group(1)).split("?utm_")[0]
        if u not in out:
            out.append(u)
    return out


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error"):
        raise ExtractError(eval_result["error"])

    bio_lines = list(eval_result.get("bio_lines") or [])
    pronouns = eval_result.get("pronouns")
    if pronouns:
        bio_lines = [line for line in bio_lines if line != pronouns]
    bio = "\n".join(bio_lines) if bio_lines else None

    username = eval_result.get("username")
    is_verified = eval_result.get("verified")
    is_private = eval_result.get("private")
    follower_count = eval_result.get("followers")
    following_count = eval_result.get("following")
    avatar_url = eval_result.get("avatar")

    raw: dict = {"extractor": eval_result}
    if pronouns:
        raw["pronouns"] = pronouns

    body = _web_profile_info(captured)
    user = None
    if body is not None:
        raw["web_profile_info"] = body
        user = body["data"]["user"]
    else:
        user = _graphql_user(captured, username)
        if user is not None:
            raw["graphql_user"] = user
    if user is not None:
        if user.get("is_verified") is not None:
            is_verified = user["is_verified"]
        if user.get("is_private") is not None:
            is_private = user["is_private"]
        for key in ("edge_followed_by",):
            if (user.get(key) or {}).get("count") is not None:
                follower_count = user[key]["count"]
        if user.get("follower_count") is not None:
            follower_count = user["follower_count"]
        if (user.get("edge_follow") or {}).get("count") is not None:
            following_count = user["edge_follow"]["count"]
        if user.get("following_count") is not None:
            following_count = user["following_count"]
        hd = user.get("profile_pic_url_hd") or (user.get("hd_profile_pic_url_info") or {}).get(
            "url"
        )
        if hd:
            avatar_url = hd

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
        links=_clean_links(list(eval_result.get("links") or []), username) or None,
        is_private=is_private,
        is_verified=is_verified,
        follower_count=follower_count,
        following_count=following_count,
        mutual_count=eval_result.get("mutual_count"),
        avatar_url=avatar_url,
        raw=raw,
    )
