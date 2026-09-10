"""A private review must preserve evidence and never execute source content."""

import json
import re

import pytest

from build_review import build_page


def test_round_trip_and_unsafe_images(tmp_path):
    payload = '</script><img src=x onerror=alert(1)> & "quoted"'
    group = {
        "current_people": [{"id": "person-1", "name": payload, "circles": '["Circle one"]'}],
        "google_candidates": [{"id": "google_contacts:1", "name": "Example Person"}],
        "profiles": [
            {
                "record_id": "instagram:1",
                "display_name": payload,
                "avatar_r2_key": "photos/records/example.jpg",
            }
        ],
    }
    source = tmp_path / "context.json"
    source.write_text(json.dumps({"Example": group}))
    page = build_page([("Batch 2", source)], {"photos/records/example.jpg": "photos/abc.img"})
    encoded = re.search(
        r'<script id="review-data" type="application/json">(.*?)</script>', page, re.S
    ).group(1)
    data = json.loads(encoded)
    assert data["groups"][0]["profiles"][0]["display_name"] == payload
    assert data["groups"][0]["current_people"][0]["circles"] == '["Circle one"]'
    assert payload not in page
    assert data["photos"] == {"photos/records/example.jpg": "photos/abc.img"}
    assert (
        data["snapshot_id"]
        == json.loads(
            re.search(
                r'<script id="review-data" type="application/json">(.*?)</script>',
                build_page([("Batch 2", source)], {}),
                re.S,
            ).group(1)
        )["snapshot_id"]
    )
    for unsafe in ("https://remote.example/photo", "../secrets", "data:text/html,bad"):
        with pytest.raises(ValueError, match="photo path"):
            build_page([("Batch 2", source)], {"photos/records/example.jpg": unsafe})
