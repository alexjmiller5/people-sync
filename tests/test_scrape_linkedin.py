import pytest

from people_sync.scrape import linkedin
from people_sync.scrape.profile import ExtractError

# Synthetic fixture in the shape the extractor JS returns (recon-linkedin.md).
FIXTURE = {
    "name": "Test Person",
    "pronouns": "He/Him",
    "headline": "Data Engineer @ TestCo",
    "location": "Greater Testville Area",
    "orgs": ["TestCo", "Test University"],
    "mutual_text": "Testa, Testb and 22 other mutual connections",
    "connections": "500+ connections",
    "followers": "1,666 followers",
    "highlights": ["You both studied at Test University from Sep 1, 2022 to May 1, 2024"],
    "about": "Sample about",
    "avatar": "https://example.invalid/a.jpg",
    "path": "/in/test-person-123/",
}


def test_parse_raises_on_the_no_main_sentinel():
    with pytest.raises(ExtractError, match="no-main"):
        linkedin.parse({"error": "no-main", "title": "LinkedIn"})


def test_parse_maps_the_top_card():
    p = linkedin.parse(FIXTURE, captured=[])

    assert p.platform == "linkedin"
    assert p.platform_id == "test-person-123"
    assert p.profile_url == "https://www.linkedin.com/in/test-person-123/"
    assert p.display_name == "Test Person"
    assert p.bio == "Data Engineer @ TestCo\nSample about"
    assert p.location == "Greater Testville Area"
    assert p.work == ["TestCo"]
    assert p.education == ["Test University"]
    assert p.follower_count == 1666
    assert p.mutual_count == 24
    assert p.avatar_url == "https://example.invalid/a.jpg"
    assert p.raw["extractor"] is FIXTURE
    assert "voyager" not in p.raw


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Testa and Testb are mutual connections", 2),
        ("Testa is a mutual connection", 1),
        ("1 mutual connection", 1),
        ("Testa, Testb and 22 other mutual connections", 24),
        (None, None),
        ("500+ connections", None),
    ],
)
def test_mutual_count_reads_every_phrasing(text, expected):
    assert linkedin._mutual_count(text) == expected


def test_captured_voyager_bodies_with_positions_are_kept_verbatim():
    captured = [
        {
            "url": "https://www.linkedin.com/voyager/api/graphql?x",
            "body": '{"included":[{"$type":"a.Position"}]}',
        },
        {
            "url": "https://www.linkedin.com/voyager/api/graphql?y",
            "body": '{"included":[{"$type":"a.Other"}]}',
        },
    ]
    p = linkedin.parse(FIXTURE, captured=captured)
    assert [c["url"][-1] for c in p.raw["voyager"]] == ["x"]


def test_extractor_js_uses_the_main_element_and_the_display_photo():
    assert 'querySelector("main")' in linkedin.EXTRACTOR_JS
    assert "profile-displayphoto" in linkedin.EXTRACTOR_JS
    assert "no-main" in linkedin.EXTRACTOR_JS
