"""Spotify user connections and profile headers from the signed-in web player."""

import json
import re
import time
from urllib.parse import quote, unquote

from people_sync import ledger
from people_sync.scrape.profile import ExtractError, Profile

URL = "https://open.spotify.com/user/{handle}"
CAPTURE: list[str] = []
READY_JS = "!!document.querySelector('main [data-testid=entityTitle] h1')"
ME_JS = """(() => [...document.querySelectorAll('a[role=menuitem]')]
    .find(a => a.textContent.trim() === 'Profile')?.getAttribute('href') || null)()"""
COUNTS_JS = r"""(() => {
  const count = kind => {
    const a = [...document.querySelectorAll('main a')].find(a =>
      a.getAttribute('href') === location.pathname + '/' + kind && /\d/.test(a.textContent));
    return a ? Number(a.textContent.match(/[\d,]+/)[0].replaceAll(',', '')) : 0;
  };
  return {followers: count('followers'), following: count('following')};
})()"""
LIST_ENTRIES_JS = r"""(() => [...document.querySelectorAll('main [data-encore-id=card] a')]
  .filter(a => /^\/(user|artist)\/[^/]+$/.test(a.getAttribute('href')))
  .map(a => ({href: a.getAttribute('href'), name: a.textContent.trim(),
    avatar: a.closest('[data-encore-id=card]').querySelector('img')?.src || null})))()"""
SCROLL_JS = """(() => {
  const e = document.querySelector('main')?.closest('[data-overlayscrollbars-viewport]');
  if (!e) return false;
  e.scrollBy(0, e.clientHeight * 0.8);
  return true;
})()"""
EXTRACTOR_JS = r"""(() => {
  const h = document.querySelector('main [data-testid=entityTitle] h1');
  if (!h || !/^\/user\/[^/]+$/.test(location.pathname)) return {error:'no-profile'};
  const count = kind => {
    const a = [...document.querySelectorAll('main a')].find(a =>
      a.getAttribute('href') === location.pathname + '/' + kind && /\d/.test(a.textContent));
    return a ? Number(a.textContent.match(/[\d,]+/)[0].replaceAll(',', '')) : 0;
  };
  return {name:h.textContent.trim(), path:location.pathname,
    avatar:document.querySelector('main [data-testid=user-image] img')?.src || null,
    followers:count('followers'), following:count('following')};
})()"""


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    path = eval_result.get("path", "")
    if (
        eval_result.get("error")
        or not re.fullmatch(r"/user/[^/]+", path)
        or not eval_result.get("name")
    ):
        raise ExtractError("no-profile")
    return Profile(
        platform="spotify",
        platform_id=unquote(path.split("/")[-1]),
        profile_url="https://open.spotify.com" + path,
        display_name=eval_result["name"],
        follower_count=eval_result.get("followers"),
        following_count=eval_result.get("following"),
        avatar_url=eval_result.get("avatar"),
        raw=eval_result,
    )


def list_users(browser) -> list[dict]:
    browser.navigate("https://open.spotify.com", 12000)
    if not browser.wait_for("!!document.querySelector('[data-testid=user-widget-link]')", 15):
        raise ExtractError("not-signed-in")
    browser.click('[data-testid="user-widget-link"]')
    if not browser.wait_for(ME_JS, 10):
        raise ExtractError("no-profile-menu")
    path = browser.eval(ME_JS)
    if not re.fullmatch(r"/user/[^/]+", path or ""):
        raise ExtractError("no-own-profile")
    browser.navigate("https://open.spotify.com" + path, 12000)
    if not browser.wait_for(READY_JS, 15):
        raise ExtractError("no-own-profile")
    counts = browser.eval(COUNTS_JS)
    merged = {}
    for kind, flag in (("followers", "follows_me"), ("following", "i_follow")):
        browser.navigate("https://open.spotify.com" + path + "/" + kind, 12000)
        seen = {}
        unchanged = 0
        for _ in range(200):
            time.sleep(2)
            entries = browser.eval(LIST_ENTRIES_JS)
            entries = json.loads(entries) if isinstance(entries, str) else entries
            before = len(seen)
            seen.update({e["href"]: e for e in entries})
            if len(seen) >= counts[kind]:
                break
            unchanged = unchanged + 1 if len(seen) == before else 0
            browser.eval(SCROLL_JS)
            if unchanged >= 4:
                raise ExtractError("incomplete-connection-list")
        if len(seen) != counts[kind]:
            raise ExtractError("incomplete-connection-list")
        for href, entry in seen.items():
            if not href.startswith("/user/"):
                continue
            uid = unquote(href.split("/")[-1])
            row = merged.setdefault(uid, {**entry, "id": uid, "follows_me": 0, "i_follow": 0})
            row[flag] = 1
            if entry.get("avatar"):
                row["avatar"] = entry["avatar"]
    return list(merged.values())


def ingest_entries(entries: list[dict]) -> dict:
    return ledger.upsert(
        [
            ledger.Record(
                source="spotify",
                source_id=e["id"],
                handle=quote(e["id"], safe=""),
                name=e["name"],
                raw={"url": URL.format(handle=quote(e["id"], safe="")), "avatar": e.get("avatar")},
                follows_me=e["follows_me"],
                i_follow=e["i_follow"],
            )
            for e in entries
        ]
    )
