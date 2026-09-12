"""Profile-only input boundary. No page/session dumps and no list acquisition."""

import json
import re
from urllib.parse import unquote, urlsplit

from people_sync import captures

POLICY = "profile-input-v1"
EXCLUSIONS = [
    POLICY,
    "DOM: only declared profile regions; scripts, styles, hidden/form/editable material, navigation, dialogs, feeds and attributes other than href/src/alt excluded",
    "API: only matching Instagram users and LinkedIn publicIdentifier profiles with nested professional fields; other users, timelines, messages, payments, session state and unsupported response schemas excluded",
    "fields: source-specific allowlists; unknown fields, contact-like or ambiguous text and unsafe URLs excluded; duplicate fields use filtered decoded input; malformed structured input excluded when privacy cannot be established",
    "unloaded content and list pages excluded; DOM is an ordered tree, not original HTML",
]

# Never fall back to body/main: these roots must not include activity or event lists.
SCOPES = {
    "instagram": "main header",
    "facebook": '[role=main] h1, [role=main] [aria-label="Personal details"], [role=main] [aria-label="Education"], [role=main] [aria-label="Work"]',
    "linkedin": "main > section:first-of-type, main section:has(> #about), main section:has(> #experience), main section:has(> #education)",
    "partiful": 'h1, [class^=profile_] img[src*="profileImages/"], [class^=profile_] a[href^="https://www.instagram.com/"], [class^=profile_] a[href^="https://instagram.com/"]',
    "spotify": 'main [data-testid=entityTitle], main [data-testid=user-image], main a[href$="/followers"], main a[href$="/following"]',
    "strava": "h1, .athlete-profile .location, .athlete-profile .avatar",
    "venmo": "props.pageProps.otherUser (selected personal profile fields only)",
}

COLLECT_JS = r"""((selector) => { // people-sync-source-dom
  const excluded = 'script,style,noscript,template,iframe,object,embed,form,input,textarea,select,button,nav,footer,[hidden],[aria-hidden="true"],[contenteditable],[role="dialog"],[role="feed"],[role="article"]';
  const visible = e => !e.closest(excluded) && e.checkVisibility({opacityProperty:true,visibilityProperty:true});
  const walk = e => {
    if (e.nodeType === 3) return {text:e.textContent};
    if (e.nodeType !== 1 || !visible(e)) return null;
    const node = {tag:e.tagName.toLowerCase(), children:[...e.childNodes].map(walk).filter(Boolean)};
    for (const k of ['href','src','alt']) {
      const v = e.getAttribute(k);
      if (v) node[k] = k === 'alt' ? v : new URL(v, location.href).href;
    }
    return node;
  };
  return [...document.querySelectorAll(selector)].filter(visible).map(walk);
})(%s)"""

_COMMON = {"error": "error", "title": "text"}
SCHEMAS = {
    "instagram": {
        **dict.fromkeys(("username",), "id"),
        **dict.fromkeys(("full_name", "pronouns", "mutual_text"), "text"),
        **dict.fromkeys(("posts", "followers", "following", "mutual_count"), "count"),
        "bio_lines": ["text"],
        "private": "bool",
        "verified": "bool",
        "avatar": "url",
        "links": ["url"],
    },
    "facebook": {
        **dict.fromkeys(("name", "counts", "lives_in", "from"), "text"),
        **dict.fromkeys(("personal", "education", "work"), ["text"]),
        "avatar": "url",
        "restricted": "bool",
        "path": "path",
        "search": "facebook-search",
    },
    "linkedin": {
        **dict.fromkeys(
            (
                "name",
                "pronouns",
                "headline",
                "location",
                "mutual_text",
                "connections",
                "followers",
                "about",
            ),
            "text",
        ),
        "orgs": ["text"],
        "highlights": ["text"],
        "avatar": "url",
        "path": "path",
    },
    "partiful": {
        "name": "text",
        "instagram": ["instagram-id"],
        "avatar": "url",
        "events": "count",
        "birthday_month": "text",
        "path": "path",
    },
    "spotify": {
        "name": "text",
        "path": "path",
        "avatar": "url",
        "followers": "count",
        "following": "count",
    },
    "strava": {
        "name": "text",
        "location": "text",
        "avatar": "url",
        "followers": "count",
        "following": "count",
        "path": "path",
    },
    "venmo": {
        "id": "id",
        "username": "id",
        "displayName": "text",
        "profilePictureUrl": "url",
        "friendCount": "count",
        "friendStatus": "text",
        "isActive": "bool",
        "display_name": "text",
        "profile_picture_url": "url",
        "first_name": "text",
        "last_name": "text",
        "about": "text",
        "friends_count": "count",
        "friend_status": "text",
        "identity_type": "text",
        "is_active": "bool",
        "is_group": "bool",
        "date_joined": "date",
    },
}
_IG_USER = {
    **dict.fromkeys(("id", "pk", "username"), "id"),
    **dict.fromkeys(("full_name", "biography", "category_name", "business_category_name"), "text"),
    **dict.fromkeys(
        ("is_private", "is_verified", "is_business_account", "is_professional_account"), "bool"
    ),
    **dict.fromkeys(("follower_count", "following_count", "media_count"), "count"),
    **dict.fromkeys(("profile_pic_url", "profile_pic_url_hd", "external_url"), "url"),
    "edge_followed_by": {"count": "count"},
    "edge_follow": {"count": "count"},
    "hd_profile_pic_url_info": {"url": "url", "width": "count", "height": "count"},
    "pronouns": ["text"],
    "bio_links": [{"url": "url", "title": "text", "link_type": "text"}],
}
_ROW = {"name": "text", "last_seen": "relative-time", "shared_events": "count", "thumb": "url"}
SCHEMAS["linkedin"].update(connections="count-text", followers="count-text")
SCHEMAS["facebook"]["counts"] = "count-text"
_LINKEDIN_PROFILE = {
    "publicIdentifier": "id",
    **dict.fromkeys(
        (
            "firstName",
            "lastName",
            "headline",
            "summary",
            "locationName",
            "industryName",
            "occupation",
        ),
        "text",
    ),
    "positions": [
        {**dict.fromkeys(("companyName", "title", "description", "locationName"), "text")}
    ],
    "educations": [
        {**dict.fromkeys(("schoolName", "degreeName", "fieldOfStudy", "description"), "text")}
    ],
}
_PATHS = {
    "instagram": r"/[A-Za-z0-9._]+/?",
    "linkedin": r"/in/[A-Za-z0-9_-]+/?",
    "facebook": r"/(?:[A-Za-z0-9.]+)/?",
    "partiful": r"/u/[A-Za-z0-9_-]+/?",
    "spotify": r"/user/[^/]+",
    "strava": r"/athletes/[0-9]+/?",
    "venmo": r"/u/[A-Za-z0-9_-]+/?",
}
_HOSTS = {
    "instagram": {"instagram.com", "www.instagram.com"},
    "linkedin": {"linkedin.com", "www.linkedin.com"},
    "facebook": {"facebook.com", "www.facebook.com"},
    "partiful": {"partiful.com"},
    "spotify": {"open.spotify.com"},
    "strava": {"strava.com", "www.strava.com"},
    "venmo": {"account.venmo.com"},
}


def _identity(value, source):
    captures._require(isinstance(value, str) and bool(value))
    decoded = unquote(value)
    pattern = r"[\w .-]+" if source == "spotify" else r"[A-Za-z0-9._-]+"
    captures._require(re.fullmatch(pattern, decoded) and decoded not in {".", ".."})
    return value


def safe_url(value, source):
    """Typed canonical profile URLs only; all other URLs keep the strict text boundary."""
    captures._require(isinstance(value, str))
    u = urlsplit(value)
    captures._require(not re.search(r"[\x00-\x20\x7f?#]", value))
    captures._require(
        u.scheme == "https"
        and u.hostname
        and not (u.username or u.password or u.query or u.fragment or u.port)
    )
    if u.netloc in _HOSTS.get(source, ()) and re.fullmatch(_PATHS[source], u.path):
        _identity(u.path.rstrip("/").rsplit("/", 1)[-1], source)
    else:
        captures._check_export_value(value)
    return value


def _value(value, kind, source):
    if value is None:
        return None
    if kind == "id" or kind == "instagram-id":
        return _identity(value, "instagram" if kind == "instagram-id" else source)
    if kind == "url":
        return safe_url(value, source)
    if kind == "count":
        captures._require(type(value) is int and 0 <= value <= 2**53 - 1)
    elif kind == "bool":
        captures._require(type(value) is bool)
    elif kind == "count-text":
        captures._require(
            isinstance(value, str)
            and re.fullmatch(
                r"[\d,.KkMm]+\+? (?:connections|followers|friends)(?: • [\d,]+ mutual)?", value
            )
        )
    elif kind == "path":
        captures._require(isinstance(value, str) and re.fullmatch(_PATHS[source], value))
        _identity(value.rstrip("/").rsplit("/", 1)[-1], source)
    elif kind == "facebook-search":
        captures._require(isinstance(value, str) and re.fullmatch(r"(?:\?id=\d+)?", value))
    elif kind == "date":
        from datetime import datetime

        captures._require(
            isinstance(value, str)
            and re.fullmatch(r"\d{4}-\d\d-\d\d(?:T\d\d:\d\d:\d\d(?:\.\d+)?Z?)?", value)
        )
        datetime.fromisoformat(value)
    elif (
        kind == "relative-time"
        and isinstance(value, str)
        and re.fullmatch(r"\d+ (?:day|week|month|year)s? ago", value)
    ):
        pass
    elif kind == "error":
        captures._require(value in {"unavailable", "no-profile", "no-header", "no-main"})
    else:
        captures._require(isinstance(value, str))
        captures._check_export_value(value)
        captures._require(
            not re.search(r"\b(?:bearer|password|csrf|access.token|session.token)\b", value, re.I)
        )
    return value


def _filter(value, schema, source, excluded):
    """Filter at acquisition; revalidation compares against this exact deterministic boundary."""
    if isinstance(schema, dict):
        if not isinstance(value, dict):
            raise ValueError("invalid profile structure")
        out = {}
        for key, item in value.items():
            if key not in schema:
                excluded.add("unknown-fields")
                continue
            try:
                out[key] = _filter(item, schema[key], source, excluded)
            except (ValueError, TypeError):
                excluded.add("unsafe-or-invalid-values")
        return out
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise ValueError("invalid profile list")
        out = []
        for item in value:
            try:
                out.append(_filter(item, schema[0], source, excluded))
            except (ValueError, TypeError):
                excluded.add("unsafe-or-invalid-values")
        return out
    return _value(value, schema, source)


def _dom(nodes, source, excluded):
    captures._require(isinstance(nodes, list))
    out = []
    for node in nodes:
        captures._require(isinstance(node, dict))
        if set(node) == {"text"}:
            try:
                out.append({"text": _value(node["text"], "text", source)})
            except (ValueError, TypeError):
                excluded.add("unsafe-or-invalid-values")
        else:
            captures._require(node.keys() <= {"tag", "children", "href", "src", "alt"})
            captures._require(
                node.get("tag")
                in {
                    "div",
                    "span",
                    "a",
                    "img",
                    "h1",
                    "h2",
                    "h3",
                    "h4",
                    "p",
                    "section",
                    "header",
                    "ul",
                    "ol",
                    "li",
                    "br",
                    "strong",
                    "b",
                    "em",
                    "small",
                    "svg",
                    "path",
                }
            )
            clean = _filter(node, {"href": "url", "src": "url", "alt": "text"}, source, set())
            clean.update(tag=node["tag"], children=_dom(node.get("children", []), source, excluded))
            if any(k not in clean for k in node if k in {"href", "src", "alt"}):
                excluded.add("unsafe-or-invalid-values")
            out.append(clean)
    return out


def incomplete(platform, reason):
    return {
        "format": "profile-dom-v1",
        "selector": SCOPES[platform],
        "status": "partial",
        "nodes": [],
        "exclusions": [],
        "reason": reason,
    }


def collect(browser, platform: str) -> dict:
    """Scoped ordered DOM nodes, or explicit partial failure; never exception/page text."""
    result = incomplete(platform, "collection-failed")
    if platform == "venmo":
        return result | {"reason": "selected-otherUser-only"}
    try:
        excluded = set()
        nodes = _dom(browser.eval(COLLECT_JS % json.dumps(SCOPES[platform])), platform, excluded)
        result.update(nodes=nodes, exclusions=sorted(excluded))
        if nodes:
            result["status"] = "partial" if excluded else "success"
            del result["reason"]
        else:
            result["reason"] = "scope-missing"
    except Exception:
        result["reason"] = "collection-failed"
    return result


def _responses(source, record_id, responses, excluded):
    if not responses:
        return []
    from people_sync.scrape import instagram

    out = []
    for entry in responses:
        try:
            u = urlsplit(entry["url"])
            captures._require(
                source in {"instagram", "linkedin"}
                and u.netloc in _HOSTS[source]
                and u.scheme == "https"
                and not (u.username or u.fragment)
            )
            handle = record_id.split(":", 1)[1]
            body = json.loads(entry["body"])
            if source == "linkedin":
                captures._require(u.path == "/voyager/api/graphql")
                users = _linkedin_users(body, handle)
                captures._require(bool(users))
                safe = [_filter(user, _LINKEDIN_PROFILE, source, excluded) for user in users]
                out.append(
                    {
                        "url": "https://www.linkedin.com/voyager/api/graphql",
                        "body": captures.encode({"included": safe}).decode(),
                    }
                )
                excluded.add("response-envelope-and-nonprofile-fields")
                continue
            kind = "web_profile_info" if "web_profile_info" in u.path else "graphql/query"
            captures._require(kind in u.path)
            user = (
                body.get("data", {}).get("user")
                if kind == "web_profile_info"
                else instagram._find_user(body, handle)
            )
            captures._require(isinstance(user, dict) and user.get("username") == handle)
            safe = _filter(user, _IG_USER, source, excluded)
            out.append(
                {
                    "url": f"https://www.instagram.com/{kind}",
                    "body": captures.encode({"data": {"user": safe}}).decode(),
                }
            )
            excluded.add("response-envelope-and-nonprofile-fields")
        except (ValueError, TypeError, KeyError, AttributeError):
            excluded.add("unsupported-or-unmatched-responses")
    return out


def _linkedin_users(node, handle, depth=0):
    if depth > 12:
        return []
    if isinstance(node, dict):
        if node.get("publicIdentifier") == handle:
            return [node]
        return [
            user for value in node.values() for user in _linkedin_users(value, handle, depth + 1)
        ]
    if isinstance(node, list):
        return [user for value in node for user in _linkedin_users(value, handle, depth + 1)]
    return []


def prepare(platform, record_id, raw_eval, captured, *, context=None):
    """The exact safe payload used for both verified retention and subsequent parsing."""
    schema = SCHEMAS[platform] | _COMMON
    captures._require(isinstance(record_id, str) and record_id.startswith(platform + ":"))
    _identity(record_id.split(":", 1)[1], platform)
    excluded = set()
    original = raw_eval
    duplicates = False

    def pairs(items):
        nonlocal duplicates
        duplicates |= len(dict(items)) != len(items)
        return dict(items)

    if isinstance(raw_eval, str):
        try:
            raw_eval = json.loads(raw_eval, object_pairs_hook=pairs)
            captures.encode(raw_eval)
        except (ValueError, RecursionError):
            # A malformed structure has no trustworthy field boundary. Only
            # unstructured, privacy-checked failure text can survive verbatim.
            raw_eval = original if not re.search(r'["\':]', original) else None
            if raw_eval is None:
                excluded.add("unsafe-or-invalid-values")
    if duplicates:
        excluded.add("duplicate-fields")
    if isinstance(raw_eval, dict):
        raw_eval = _filter(raw_eval, schema, platform, excluded)
    elif raw_eval is not None:
        try:
            _value(raw_eval, "text", platform)
        except (ValueError, TypeError):
            raw_eval = None
            excluded.add("unsafe-or-invalid-values")
    payload = {"eval": raw_eval, "captured": _responses(platform, record_id, captured, excluded)}
    if isinstance(original, str) and raw_eval is not None:
        try:
            unchanged = not duplicates and (
                original == raw_eval or json.loads(original) == raw_eval
            )
        except ValueError:
            unchanged = original == raw_eval
        payload["raw_eval"] = original if unchanged else captures.encode(raw_eval).decode()
    ctx = dict(context or {})
    prior = ctx.pop("exclusions", [])
    captures._require(
        isinstance(prior, list)
        and all(
            x
            in {
                "unknown-fields",
                "unsafe-or-invalid-values",
                "response-envelope-and-nonprofile-fields",
                "unsupported-or-unmatched-responses",
                "duplicate-fields",
            }
            for x in prior
        )
    )
    excluded.update(prior)
    failure = ctx.pop("failure", None)
    captures._require(
        failure in {None, "readiness-failed", "enrichment-failed", "challenge", "extraction-failed"}
    )
    dom = ctx.pop("source_dom", None)
    if dom is not None:
        captures._require(
            isinstance(dom, dict)
            and dom.keys() <= {"format", "selector", "status", "nodes", "exclusions", "reason"}
        )
        captures._require(dom["format"] == "profile-dom-v1" and dom["selector"] == SCOPES[platform])
        captures._require(
            dom["status"] in {"success", "partial"}
            and dom.get("reason")
            in {
                None,
                "selected-otherUser-only",
                "scope-missing",
                "collection-failed",
                "collection-not-reached",
            }
        )
        captures._require(dom["exclusions"] in [[], ["unsafe-or-invalid-values"]])
        if dom["status"] == "success":
            captures._require(
                bool(dom["nodes"])
                and not dom.get("reason")
                and not dom["exclusions"]
                and platform != "venmo"
            )
        if dom.get("reason"):
            captures._require(dom["status"] == "partial" and not dom["nodes"])
        if platform == "venmo":
            captures._require(dom.get("reason") == "selected-otherUser-only" and not dom["nodes"])
        clean_nodes = _dom(dom["nodes"], platform, excluded)
        captures._require(clean_nodes == dom["nodes"])
    safe_context = _filter(ctx, _ROW if platform == "partiful" else {}, platform, excluded)
    if dom is not None:
        safe_context["source_dom"] = dom
    if failure is not None:
        safe_context["failure"] = failure
    safe_context["exclusions"] = sorted(excluded)
    payload["context"] = safe_context
    return payload


def completeness(payload):
    context = payload["context"]
    dom = context.get("source_dom")
    if context.get("failure") or (dom and dom["status"] == "partial"):
        return "partial"
    return "privacy-filtered" if dom else "extracted-only"


def validate(platform, record_id, payload):
    captures._require(
        payload
        == prepare(
            platform,
            record_id,
            payload.get("raw_eval", payload["eval"]),
            payload["captured"],
            context=payload.get("context"),
        )
    )
