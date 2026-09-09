import pytest

from people_sync.scrape import venmo
from people_sync.scrape.profile import ExtractError


def test_personal_profile_keeps_identity_and_picture_without_contact_or_payment_data():
    result = venmo.parse(
        {
            "id": "123456789",
            "username": "example-person",
            "displayName": "Example Person",
            "profilePictureUrl": "https://pics-v3.venmo.com/example",
            "friendCount": 12,
            "friendStatus": "friend",
            "isActive": True,
        }
    )
    assert result.platform_id == "123456789"
    assert result.profile_url == "https://account.venmo.com/u/example-person"
    assert result.display_name == "Example Person"
    assert result.avatar_url == "https://pics-v3.venmo.com/example"
    assert result.raw["friendStatus"] == "friend"
    assert result.follower_count is None  # friends are not followers


@pytest.mark.parametrize("value", [{}, {"error": "no-profile"}, {"id": "123"}])
def test_missing_personal_profile_is_not_a_blank_success(value):
    with pytest.raises(ExtractError, match="no-profile"):
        venmo.parse(value)
