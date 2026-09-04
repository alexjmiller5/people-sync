import json

from contact_sync.scrape.profile import Profile, upsert_profile


def _profile(**overrides) -> Profile:
    defaults = dict(
        record_id="instagram:alice123",
        platform="instagram",
        profile_url="https://www.instagram.com/alice123/",
        platform_id="alice123",
        display_name="Alice Test",
        bio="hello",
        links=["https://example.invalid/a"],
        is_private=False,
        is_verified=True,
        follower_count=10,
        following_count=5,
        mutual_count=2,
        avatar_url="https://example.invalid/avatar.jpg",
    )
    defaults.update(overrides)
    return Profile(**defaults)


def test_insert_when_no_existing_row(mocker):
    mocker.patch("contact_sync.lifedata.sql", return_value=[])
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-04T00:00:00.000Z")
    insert = mocker.patch("contact_sync.lifedata.insert")

    upsert_profile(
        _profile(),
        "photos/records/instagram/instagram_alice123-aaaaaaaa.jpg",
        "sha-value",
        "profiles/instagram/instagram_alice123/2026-09-04T00:00:00.000Z.json",
    )

    table, rows = insert.call_args.args
    assert table == "contact_profiles"
    row = rows[0]
    assert row["id"] == "instagram:alice123"
    assert row["record_id"] == "instagram:alice123"
    assert row["platform"] == "instagram"
    assert row["profile_url"] == "https://www.instagram.com/alice123/"
    assert row["display_name"] == "Alice Test"
    assert json.loads(row["links"]) == ["https://example.invalid/a"]
    assert row["education"] is None
    assert row["work"] is None
    assert row["is_private"] == 0
    assert row["is_verified"] == 1
    assert row["follower_count"] == 10
    assert row["avatar_r2_key"] == "photos/records/instagram/instagram_alice123-aaaaaaaa.jpg"
    assert row["avatar_sha256"] == "sha-value"
    assert (
        row["raw_r2_key"] == "profiles/instagram/instagram_alice123/2026-09-04T00:00:00.000Z.json"
    )
    assert row["scraped_at"] == "2026-09-04T00:00:00.000Z"


def test_insert_serializes_education_and_work_lists(mocker):
    mocker.patch("contact_sync.lifedata.sql", return_value=[])
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-04T00:00:00.000Z")
    insert = mocker.patch("contact_sync.lifedata.insert")

    upsert_profile(
        _profile(education=["Some University"], work=["Some Company"]),
        None,
        None,
        None,
    )

    row = insert.call_args.args[1][0]
    assert json.loads(row["education"]) == ["Some University"]
    assert json.loads(row["work"]) == ["Some Company"]
    assert row["avatar_r2_key"] is None


def test_update_when_existing_row_emits_update_not_insert(mocker):
    sql = mocker.patch("contact_sync.lifedata.sql", return_value=[{"id": "instagram:alice123"}])
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-04T00:00:00.000Z")
    insert = mocker.patch("contact_sync.lifedata.insert")

    upsert_profile(_profile(display_name="Alice Updated"), None, None, None)

    insert.assert_not_called()
    assert sql.call_count == 2
    select_stmt = sql.call_args_list[0].args[0]
    assert "SELECT id FROM contact_profiles" in select_stmt
    assert "record_id = 'instagram:alice123'" in select_stmt

    update_stmt = sql.call_args_list[1].args[0]
    assert update_stmt.startswith("UPDATE contact_profiles SET")
    assert "display_name = 'Alice Updated'" in update_stmt
    assert "is_private = 0" in update_stmt
    assert "is_verified = 1" in update_stmt
    assert "follower_count = 10" in update_stmt
    assert "avatar_r2_key = NULL" in update_stmt
    assert "scraped_at = '2026-09-04T00:00:00.000Z'" in update_stmt
    assert update_stmt.endswith("WHERE record_id = 'instagram:alice123'")


def test_update_quotes_apostrophes_in_text_fields(mocker):
    mocker.patch("contact_sync.lifedata.sql", return_value=[{"id": "instagram:alice123"}])
    mocker.patch("contact_sync.lifedata.now_iso", return_value="2026-09-04T00:00:00.000Z")
    sql = mocker.patch("contact_sync.lifedata.sql", return_value=[{"id": "instagram:alice123"}])

    upsert_profile(_profile(bio="it's a test"), None, None, None)

    update_stmt = sql.call_args_list[1].args[0]
    assert "bio = 'it''s a test'" in update_stmt
