import json

import pytest

from people_sync.scrape import facebook
from people_sync.scrape.profile import ExtractError

FIXTURE = {
    "name": "Test Person",
    "counts": "457 friends • 179 mutual",
    "personal": [
        "Lives in Testville, Michigan",
        "From Sampletown, Connecticut",
        "December 9, 2002",
        "Female",
    ],
    "education": ["Test University", "Class of 2025", "See more education"],
    "work": ["TestCo", "Works at TestCo", "Old Corp"],
    "lives_in": "Lives in Testville, Michigan",
    "from": "From Sampletown, Connecticut",
    "avatar": "https://example.invalid/a.jpg",
    "restricted": False,
    "path": "/test.person.1",
    "search": "",
}


def test_parse_raises_on_no_main():
    with pytest.raises(ExtractError, match="no-main"):
        facebook.parse({"error": "no-main"})


def test_parse_maps_personal_details_and_counts():
    p = facebook.parse(FIXTURE)
    assert p.platform == "facebook"
    assert p.platform_id == "test.person.1"
    assert p.profile_url == "https://www.facebook.com/test.person.1"
    assert p.display_name == "Test Person"
    assert p.location == "Testville, Michigan"
    assert p.hometown == "Sampletown, Connecticut"
    assert p.birthday == "2002-12-09"
    assert p.education == ["Test University"]
    assert p.work == ["TestCo", "Old Corp"]
    assert p.follower_count == 457
    assert p.mutual_count == 179
    assert p.avatar_url == "https://example.invalid/a.jpg"
    assert p.is_private is None


def test_numeric_profile_ids_keep_the_profile_php_form():
    p = facebook.parse({**FIXTURE, "path": "/profile.php", "search": "?id=1234567"})
    assert p.platform_id == "profile.php?id=1234567"
    assert p.profile_url == "https://www.facebook.com/profile.php?id=1234567"


def test_month_day_only_birthday_keeps_the_year_open():
    p = facebook.parse({**FIXTURE, "personal": ["December 9", "Male"]})
    assert p.birthday == "--12-09"


def test_no_birthday_line_is_none():
    p = facebook.parse({**FIXTURE, "personal": ["Lives in X", "Female"]})
    assert p.birthday is None


def test_assign_handles_only_unique_exact_names():
    entries = [
        {"handle": "a.one", "name": "Ann One", "mutual_text": "3 mutual friends"},
        {"handle": "b.two", "name": "Bob Two", "mutual_text": None},
        {"handle": "b.two.2", "name": "Bob Two", "mutual_text": None},
        {"handle": "c.three", "name": "Cy Three", "mutual_text": None},
        {"handle": "d.four", "name": "Di Four", "mutual_text": None},
    ]
    records = [
        {"id": "facebook:1", "name": "ann one", "handle": None},
        {"id": "facebook:2", "name": "Bob Two", "handle": None},
        {"id": "facebook:3", "name": "Cy Three", "handle": None},
        {"id": "facebook:3b", "name": "Cy Three", "handle": None},
        {"id": "facebook:4", "name": "Di Four", "handle": "already"},
    ]
    assert facebook.assign_handles(entries, records) == [{"id": "facebook:1", "handle": "a.one"}]


def test_list_friends_scrolls_until_the_link_count_settles():
    class FakeBrowser:
        def __init__(self):
            self.counts = iter([10, 20, 30, 30, 30, 99])
            self.scrolls = 0

        def navigate(self, url, wait_ms):
            self.url = url

        def scroll(self, px):
            self.scrolls += 1

        def eval(self, js):
            if js == facebook.LIST_LINK_COUNT_JS:
                return next(self.counts)
            return json.dumps([{"handle": "x", "name": "X Y", "mutual_text": None}])

    b = FakeBrowser()
    entries = facebook.list_friends(b, settle_s=0)
    assert b.url == facebook.LIST_URL
    assert b.scrolls == 5
    assert entries == [{"handle": "x", "name": "X Y", "mutual_text": None}]


def test_list_command_assigns_handles(mocker, capsys):
    from people_sync import cli

    mocker.patch("people_sync.scrape.cdp.Browser.connect", return_value=mocker.Mock())
    mocker.patch(
        "people_sync.scrape.facebook.list_friends",
        return_value=[{"handle": "a.one", "name": "Ann One", "mutual_text": None}],
    )
    sql = mocker.patch("people_sync.cli.lifedata.sql")
    sql.side_effect = [
        [
            {"id": "facebook:1", "name": "Ann One", "handle": None},
            {"id": "facebook:2", "name": "Bo", "handle": None},
        ],
        [],
    ]

    cli.main(["list", "facebook", "--endpoint", "127.0.0.1:1"])

    assert "UPDATE people_sync_records SET handle = 'a.one'" in sql.call_args_list[1].args[0]
    assert json.loads(capsys.readouterr().out) == {
        "entries": 1,
        "assigned": 1,
        "unmatched_records": 1,
    }
