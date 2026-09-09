from unittest.mock import Mock

import pytest

from people_sync.scrape import spotify
from people_sync.scrape.profile import ExtractError


def test_connection_lists_keep_user_ids_and_both_directions(mocker):
    browser = Mock()
    browser.wait_for.return_value = True
    browser.eval.side_effect = [
        "/user/owner",
        {"followers": 1, "following": 2},
        [{"href": "/user/test%231", "name": "Test Friend", "avatar": None}],
        [
            {"href": "/artist/music", "name": "Artist"},
            {
                "href": "/user/test%231",
                "name": "Test Friend",
                "avatar": "https://example.test/a.jpg",
            },
        ],
    ]
    mocker.patch.object(spotify.time, "sleep")
    rows = spotify.list_users(browser)
    assert len(rows) == 1
    assert rows[0]["id"] == "test#1"
    assert rows[0]["follows_me"] == rows[0]["i_follow"] == 1
    write = mocker.patch("people_sync.ledger.upsert", return_value={"new": 1})
    spotify.ingest_entries(rows)
    record = write.call_args.args[0][0]
    assert record.source_id == "test#1"
    assert record.handle == "test%231"


def test_profile_and_incomplete_lists_fail_without_writes(mocker):
    profile = spotify.parse(
        {
            "path": "/user/test%231",
            "name": "Test Friend",
            "avatar": None,
            "followers": 0,
            "following": 12,
        }
    )
    assert profile.platform_id == "test#1"
    assert profile.profile_url.endswith("test%231")
    assert profile.follower_count == 0
    assert profile.avatar_url is None
    with pytest.raises(ExtractError):
        spotify.parse({"path": "/artist/music", "name": "Artist"})
    browser = Mock()
    browser.wait_for.return_value = True
    browser.eval.side_effect = ["/user/owner", {"followers": 2, "following": 0}] + [[], True] * 4
    mocker.patch.object(spotify.time, "sleep")
    with pytest.raises(ExtractError, match="incomplete"):
        spotify.list_users(browser)
