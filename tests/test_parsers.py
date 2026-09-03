import pytest

from contact_sync import parsers

FIX = "tests/fixtures"


def test_instagram_merges_lists():
    recs = {
        r.source_id: r
        for r in parsers.parse_instagram(f"{FIX}/ig_followers.json", f"{FIX}/ig_following.json")
    }
    # 3 valid followers + 1 malformed (skipped) + 2 following (1 overlap) = 4 unique
    assert len(recs) == 4

    both = recs["testuser_a"]  # in both fixture files
    assert both.source == "instagram"
    assert both.follows_me == 1 and both.i_follow == 1
    assert both.raw["followers"]["string_list_data"][0]["value"] == "testuser_a"
    assert both.raw["following"]["string_list_data"][0]["value"] == "testuser_a"

    only_follower = recs["testuser_b"]  # only in followers fixture
    assert only_follower.follows_me == 1 and only_follower.i_follow == 0
    assert only_follower.raw["following"] is None

    only_following = recs["testuser_d"]  # only in following fixture
    assert only_following.follows_me == 0 and only_following.i_follow == 1

    # mixed-case username lowercased for source_id, handle keeps original case
    mixed_case = recs["testuser_c"]
    assert mixed_case.handle == "TestUser_C"


def test_instagram_skips_malformed_entry():
    recs = parsers.parse_instagram(f"{FIX}/ig_followers.json", f"{FIX}/ig_following.json")
    # the empty string_list_data entry in ig_followers.json fixture must not appear
    assert "" not in {r.source_id for r in recs}
    assert None not in {r.source_id for r in recs}


def test_facebook_maps_names_and_mutual_flags():
    recs = parsers.parse_facebook(f"{FIX}/fb_friends.json")
    assert len(recs) == 2  # 2 valid, 1 malformed (missing name) skipped

    by_id = {r.source_id: r for r in recs}
    person = by_id["test_person"]
    assert person.source == "facebook"
    assert person.name == "Test Person"
    assert person.handle is None
    assert person.follows_me == 1 and person.i_follow == 1
    assert person.raw == {"name": "Test Person", "timestamp": 1700000000}

    co_person = by_id["test_co_person"]
    assert co_person.name == "Test Co Person"


def test_snapchat_maps_username_and_display_name():
    recs = parsers.parse_snapchat(f"{FIX}/snap_friends.json")
    assert len(recs) == 2  # 2 valid, 1 malformed (missing Username) skipped

    by_id = {r.source_id: r for r in recs}
    a = by_id["testuser_a"]
    assert a.source == "snapchat"
    assert a.handle == "testuser_a"
    assert a.name == "Test User A"
    assert a.follows_me is None and a.i_follow is None

    e = by_id["testuser_e"]
    assert e.name == "Test User E"
    assert e.handle == "testuser_e"

    # only the current friends list is ingested - not deleted/blocked siblings
    assert "testuser_deleted" not in by_id


def test_linkedin_skips_preamble_and_slugs():
    recs = parsers.parse_linkedin(f"{FIX}/linkedin_connections.csv")
    assert recs[0].source == "linkedin"
    assert recs[0].source_id == "test-person-123"  # from fixture URL slug
    assert recs[0].name == "Test Person"
    assert recs[0].raw["Company"] == "TestCo"


def test_linkedin_handles_empty_email_and_skips_blank_row():
    recs = parsers.parse_linkedin(f"{FIX}/linkedin_connections.csv")
    # fixture has 3 data rows: 1 valid, 1 valid w/ empty company/position, 1 fully blank (skipped)
    assert len(recs) == 2

    by_id = {r.source_id: r for r in recs}
    jane = by_id["jane-sample-456"]
    assert jane.name == "Jane Sample"
    assert jane.raw["Email Address"] == "jane@example.com"
    assert jane.raw["Company"] == ""
    assert jane.follows_me is None and jane.i_follow is None


def test_linkedin_raises_valueerror_when_header_row_missing(tmp_path):
    csv_path = tmp_path / "no_header.csv"
    csv_path.write_text('Notes:\n"some preamble"\n\nnot,a,header,row\nfoo,bar,baz,qux\n')

    with pytest.raises(ValueError, match="linkedin header row not found"):
        parsers.parse_linkedin(str(csv_path))
