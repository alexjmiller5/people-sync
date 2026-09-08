import json

import pytest

from people_sync.scrape import strava
from people_sync.scrape.profile import ExtractError


def test_parse_maps_an_athlete_page():
    p = strava.parse(
        {
            "name": "Test Person",
            "location": "Testville, CT",
            "avatar": "https://x/a.jpg",
            "followers": 12,
            "following": 34,
            "path": "/athletes/123",
        }
    )
    assert p.platform == "strava" and p.platform_id == "123"
    assert p.profile_url == "https://www.strava.com/athletes/123"
    assert (p.display_name, p.location, p.avatar_url, p.follower_count, p.following_count) == (
        "Test Person",
        "Testville, CT",
        "https://x/a.jpg",
        12,
        34,
    )


def test_parse_raises_on_no_profile():
    with pytest.raises(ExtractError, match="no-profile"):
        strava.parse({"error": "no-profile"})


class FakeBrowser:
    def __init__(self):
        self.urls = []

    def navigate(self, url, wait_ms=None, capture=None):
        self.urls.append(url)

    def eval(self, js):
        if js == strava.ME_JS:
            return "999"
        if "followers" in self.urls[-1]:
            return json.dumps(
                [
                    {"id": "1", "name": "A One", "location": "X", "avatar": "a"},
                    {"id": "2", "name": "B Two", "location": None, "avatar": None},
                ]
            )
        return json.dumps(
            [
                {"id": "2", "name": "B Two", "location": None, "avatar": None},
                {"id": "3", "name": "C Three", "location": "Y", "avatar": "c"},
            ]
        )


def test_list_athletes_merges_both_directions(mocker):
    mocker.patch("people_sync.scrape.strava.time.sleep")
    entries = {e["id"]: e for e in strava.list_athletes(FakeBrowser())}
    assert set(entries) == {"1", "2", "3"}
    assert (entries["1"]["follows_me"], entries["1"]["i_follow"]) == (1, 0)
    assert (entries["2"]["follows_me"], entries["2"]["i_follow"]) == (1, 1)
    assert (entries["3"]["follows_me"], entries["3"]["i_follow"]) == (0, 1)


def test_ingest_entries_writes_ledger_records_with_urls(mocker):
    upsert = mocker.patch("people_sync.ledger.upsert", return_value={"new": 1, "updated": 0})
    strava.ingest_entries(
        [
            {
                "id": "1",
                "name": "A One",
                "location": "X",
                "avatar": "a",
                "follows_me": 1,
                "i_follow": 0,
            }
        ]
    )
    r = upsert.call_args.args[0][0]
    assert (r.source, r.source_id, r.handle, r.follows_me, r.i_follow) == ("strava", "1", "1", 1, 0)
    assert r.raw["url"] == "https://www.strava.com/athletes/1"
