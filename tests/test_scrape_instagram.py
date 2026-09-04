import json

from contact_sync.scrape import instagram

# Verbatim synthetic fixture from recon-instagram.md.
FIXTURE = {
    "username": "testuser_a",
    "full_name": "Test Person",
    "pronouns": "he/him",
    "posts": 1,
    "followers": 686,
    "following": 866,
    "bio_lines": ["he/him", "Sample bio line", "@testschool"],
    "private": False,
    "verified": False,
    "mutual_count": 11,
    "mutual_text": "Followed by testuser_b, testuser_c + 9 more",
    "avatar": "https://example.invalid/avatar.jpg",
    "links": ["https://www.instagram.com/testschool/"],
}


def test_parse_maps_all_fields_from_fixture():
    p = instagram.parse(FIXTURE, captured=[])

    assert p.platform == "instagram"
    assert p.profile_url == "https://www.instagram.com/testuser_a/"
    assert p.platform_id == "testuser_a"
    assert p.display_name == "Test Person"
    assert p.bio == "Sample bio line\n@testschool"
    assert p.location is None
    assert p.hometown is None
    assert p.education is None
    assert p.work is None
    assert p.birthday is None
    assert p.links == ["https://www.instagram.com/testschool/"]
    assert p.is_private is False
    assert p.is_verified is False
    assert p.follower_count == 686
    assert p.following_count == 866
    assert p.mutual_count == 11
    assert p.avatar_url == "https://example.invalid/avatar.jpg"


def test_parse_removes_pronouns_line_and_keeps_pronouns_in_raw():
    p = instagram.parse(FIXTURE, captured=[])

    assert "he/him" not in p.bio.split("\n")
    assert p.raw["pronouns"] == "he/him"
    assert p.raw["extractor"] == FIXTURE


def test_parse_without_pronouns_keeps_bio_lines_untouched():
    fixture = dict(FIXTURE, pronouns=None, bio_lines=["Sample bio line", "@testschool"])

    p = instagram.parse(fixture, captured=[])

    assert p.bio == "Sample bio line\n@testschool"
    assert "pronouns" not in p.raw


def test_parse_with_no_bio_lines_leaves_bio_none():
    fixture = dict(FIXTURE, bio_lines=[], pronouns=None)

    p = instagram.parse(fixture, captured=[])

    assert p.bio is None


def _captured_web_profile_info(**user_overrides) -> list[dict]:
    user = {
        "id": "1234567890",
        "username": "testuser_a",
        "is_verified": True,
        "edge_followed_by": {"count": 9999},
        "edge_follow": {"count": 42},
        "profile_pic_url_hd": "https://example.invalid/hd.jpg",
        "category_name": "Public Figure",
    }
    user.update(user_overrides)
    body = {"data": {"user": user}}
    return [
        {
            "url": "https://i.instagram.com/api/v1/users/web_profile_info/?username=testuser_a",
            "status": 200,
            "mimeType": "application/json",
            "body": json.dumps(body),
        }
    ]


def test_parse_prefers_captured_web_profile_info_fields():
    captured = _captured_web_profile_info()

    p = instagram.parse(FIXTURE, captured=captured)

    assert p.is_verified is True  # header said False, body wins
    assert p.follower_count == 9999
    assert p.following_count == 42
    assert p.avatar_url == "https://example.invalid/hd.jpg"
    assert p.raw["web_profile_info"]["data"]["user"]["category_name"] == "Public Figure"


def test_parse_keeps_header_fields_when_no_captured_body():
    p = instagram.parse(FIXTURE, captured=[])

    assert p.is_verified is False
    assert p.follower_count == 686
    assert p.avatar_url == "https://example.invalid/avatar.jpg"


def test_parse_ignores_captured_entries_that_are_not_json():
    captured = [
        {
            "url": "https://i.instagram.com/api/v1/users/web_profile_info/",
            "status": 200,
            "mimeType": "application/json",
            "body": "not json",
        }
    ]

    p = instagram.parse(FIXTURE, captured=captured)

    assert p.is_verified is False
    assert "web_profile_info" not in p.raw


def test_parse_ignores_captured_entries_with_no_matching_url():
    captured = [
        {
            "url": "https://example.invalid/unrelated",
            "status": 200,
            "mimeType": "application/json",
            "body": json.dumps({"data": {"user": {"is_verified": True}}}),
        }
    ]

    p = instagram.parse(FIXTURE, captured=captured)

    assert p.is_verified is False
    assert "web_profile_info" not in p.raw
