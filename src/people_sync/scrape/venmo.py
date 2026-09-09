"""Public-facing personal profile fields from the signed-in Venmo web page."""

from urllib.parse import quote

from people_sync.scrape.profile import ExtractError, Profile

URL = "https://account.venmo.com/u/{handle}"
CAPTURE: list[str] = []  # Network responses include payments and credentials.
READY_JS = "!!document.querySelector('#__NEXT_DATA__')"
EXTRACTOR_JS = """(() => {
  const el = document.querySelector('#__NEXT_DATA__');
  if (!el) return {error:'no-profile'};
  const p = JSON.parse(el.textContent).props.pageProps;
  const u = p.otherUser;
  if (p.pageType !== 'personal' || !u) return {error:'no-profile'};
  return {id:u.id, username:u.username, displayName:u.displayName,
    profilePictureUrl:u.profilePictureUrl, friendCount:u.friendCount,
    friendStatus:u.friendStatus, isActive:u.isActive};
})()"""


def parse(eval_result: dict, captured: list[dict] | None = None) -> Profile:
    if eval_result.get("error") or not all(
        eval_result.get(key) for key in ("id", "username", "displayName")
    ):
        raise ExtractError("no-profile")
    return Profile(
        platform="venmo",
        platform_id=str(eval_result["id"]),
        profile_url=URL.format(handle=quote(eval_result["username"], safe="")),
        display_name=eval_result["displayName"],
        avatar_url=eval_result.get("profilePictureUrl"),
        raw=eval_result,
    )
