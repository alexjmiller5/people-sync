import base64
import copy
import csv
import hashlib
import io
import json
from dataclasses import asdict
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    import httpx
    import subprocess
    from people_sync import photos, lifedata, ledger, notion_people
    from people_sync.scrape.cdp import Browser

    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay attempted an external operation")

    for owner, name in [
        (httpx.Client, "send"),
        (subprocess, "run"),
        (photos, "get_object"),
        (photos, "put_object"),
        (photos, "fetch_url_photo"),
        (lifedata, "sql"),
        (lifedata, "insert"),
        (ledger, "upsert"),
        (notion_people, "create_stub"),
        (Browser, "connect"),
    ]:
        monkeypatch.setattr(owner, name, forbidden)


def test_replay_is_offline_and_deterministic(monkeypatch):
    from people_sync import captures, replay, photos, lifedata

    def forbidden(*args, **kwargs):
        raise AssertionError("offline replay attempted an external operation")

    monkeypatch.setattr(photos, "get_object", forbidden)
    monkeypatch.setattr(photos, "fetch_url_photo", forbidden)
    monkeypatch.setattr(lifedata, "sql", forbidden)
    c = captures.build_capture(
        "spotify",
        "profile",
        {
            "eval": {
                "name": "Example",
                "path": "/user/example",
                "avatar": None,
                "followers": 2,
                "following": 3,
            },
            "captured": [],
        },
        record_id="spotify:example",
        captured_at="2026-01-01T00:00:00.000Z",
        completeness="extracted-only",
    )
    assert replay.replay_capture(c) == replay.replay_capture(c)
    assert replay.replay_capture(c)["profile"]["display_name"] == "Example"


def test_replay_preserves_identity_and_excludes_raw_only_from_comparison(monkeypatch):
    from people_sync import captures, replay
    from people_sync.scrape import spotify

    c = captures.build_capture(
        "spotify",
        "profile",
        {"eval": {"name": "Example", "path": "/user/example", "future": [1, 2]}, "captured": []},
        record_id="spotify:example",
        captured_at="2026-01-01T00:00:00.000Z",
    )
    original = copy.deepcopy(c)
    a = replay.replay_capture(c)
    assert a["status"] == "ok"
    assert a["record_id"] == a["profile"]["record_id"] == "spotify:example"
    assert a["captured_at"] == c["captured_at"]
    assert a["input_sha256"] == c["payload_sha256"]
    assert a["parser_fingerprint"] == captures.code_fingerprint()
    assert a["profile"]["raw"]["future"] == [1, 2]
    assert "raw" not in replay.normalized(a)["profile"]
    parser = spotify.parse

    def destructive(raw, responses):
        result = parser(raw, responses)
        raw["future"] = [3]
        return result

    monkeypatch.setattr(spotify, "parse", destructive)
    b = replay.replay_capture(c)
    assert c == original
    assert a["profile"]["raw"] != b["profile"]["raw"]
    assert replay.normalized(a) == replay.normalized(b)
    assert "extracted fields" in " ".join(a["limitations"])


@pytest.mark.parametrize(
    "source,raw,status",
    [
        ("spotify", "{broken", "malformed"),
        ("instagram", {}, "malformed"),
        ("spotify", '{"count":NaN}', "malformed"),
        ("spotify", None, "malformed"),
        ("spotify", {"error": "no-profile"}, "unavailable"),
        ("instagram", {"error": "unavailable"}, "unavailable"),
        ("spotify", {"error": "Bearer synthetic-secret"}, "parse-failed"),
    ],
)
def test_malformed_and_unavailable_are_explicit_private_results(source, raw, status):
    from people_sync import captures, replay

    c = captures.build_capture(source, "profile", {"eval": raw, "captured": []})
    result = replay.replay_capture(c)
    assert result["status"] == status
    assert "profile" not in result
    assert "synthetic-secret" not in json.dumps(result)


def test_legacy_extracted_and_parsed_captures_are_never_historically_verified():
    from people_sync import replay

    legacy = {
        "source": "spotify",
        "record_id": "spotify:example",
        "eval": {"name": "Example", "path": "/user/example"},
        "captured": [],
    }
    a = replay.replay_capture(legacy)
    assert a == replay.replay_capture(legacy)
    assert a["status"] == "ok" and a["completeness"] == "extracted-only"
    assert a["verification"] == "unverified"
    assert a["captured_at"] is None and a["capture_id"] is None
    parsed = {"platform": "spotify", "record_id": "spotify:example", "display_name": "Example"}
    b = replay.replay_capture(parsed)
    assert b["completeness"] == "legacy-parsed-only"
    assert b["status"] == "unsupported" and "profile" not in b
    missing_source = replay.replay_capture({"eval": {}, "captured": []})
    assert missing_source["status"] == "invalid"
    assert "source" in " ".join(missing_source["limitations"])


def test_tampering_does_not_dispatch_parser(monkeypatch):
    from people_sync import captures, replay
    from people_sync.scrape import spotify

    c = captures.build_capture("spotify", "profile", {"eval": {}, "captured": []})
    c["payload"]["eval"]["name"] = "Changed"
    monkeypatch.setattr(spotify, "parse", lambda *args: pytest.fail("parsed tampered capture"))
    assert replay.replay_capture(c)["status"] == "invalid"


@pytest.mark.parametrize(
    "source,files",
    [
        ("instagram", {"followers": "ig_followers.json", "following": "ig_following.json"}),
        ("facebook", {"export": "fb_friends.json"}),
        ("snapchat", {"export": "snap_friends.json"}),
    ],
)
def test_export_replays_existing_parsers_with_original_ordinals(
    source, files, tmp_path, monkeypatch
):
    from people_sync import captures, replay, parsers

    fixtures = Path("tests/fixtures")
    payload = {
        "files": [
            {
                "role": role,
                "filename": name,
                "format": "json",
                "encoding": "base64",
                "data": base64.b64encode((fixtures / name).read_bytes()).decode(),
            }
            for role, name in files.items()
        ]
    }
    c = captures.build_capture(source, "export", payload)
    before = copy.deepcopy(c)
    parser = getattr(parsers, f"parse_{source}")
    expected = [asdict(r) for r in parser(*(str(fixtures / name) for name in files.values()))]
    paths = []

    def checked(*args):
        for arg in args:
            path = Path(arg)
            paths.append(path)
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
        return parser(*args)

    monkeypatch.setattr(parsers, f"parse_{source}", checked)
    result = replay.replay_capture(c)
    assert result["status"] == "ok" and result["records"] == expected
    assert c == before and all(not p.exists() for p in paths)
    assert any(row["status"] == "skipped" for row in result["observations"])
    assert result["observations"][0]["ordinal"] == 0


def test_malformed_export_cannot_cross_privacy_boundary():
    from people_sync import captures, replay

    c = captures.build_capture(
        "facebook",
        "export",
        {
            "files": [
                {
                    "role": "export",
                    "filename": "friends.json",
                    "format": "json",
                    "encoding": "base64",
                    "data": base64.b64encode(b'{"friends_v2":[]}').decode(),
                }
            ]
        },
    )
    c["payload"]["files"][0]["data"] = base64.b64encode(b"{broken").decode()
    c["payload_sha256"] = hashlib.sha256(captures.encode(c["payload"])).hexdigest()
    with pytest.raises(ValueError):
        captures.validate(c)
    assert replay.replay_capture(c)["status"] == "invalid"
    assert base64.b64decode(c["payload"]["files"][0]["data"]) == b"{broken"


@pytest.mark.parametrize(
    "column,contact",
    [
        ("Position", "synthetic@example.invalid"),
        ("Position", "+1 (202) 555-0148"),
        ("Position", "123 Example Street"),
        ("Position", "office 123, Example Street"),
        ("Position", "office 123:Example Street"),
        ("Position", "office 123%252C%2520Example Street"),
        ("URL", "https://linkedin.com/in/example%20?access_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%2520%253Faccess_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%09?access_token=synthetic-secret"),
        ("URL", "https://linkedin.com/in/example%20#synthetic-secret"),
    ],
)
def test_offline_replay_rejects_unsafe_old_export_even_with_matching_checksum(column, contact):
    from people_sync import captures, replay

    c = captures.build_capture(
        "linkedin",
        "export",
        {
            "files": [
                {
                    "role": "export",
                    "filename": "Connections.csv",
                    "format": "csv",
                    "encoding": "base64",
                    "data": base64.b64encode(
                        b"First Name,Last Name,URL,Company,Position\nExample,Person,https://linkedin.com/in/example,Example Co,Engineer\n"
                    ).decode(),
                }
            ]
        },
    )
    # Simulate a retained envelope from the old collector; hashing it is not privacy validation.
    data = io.StringIO()
    writer = csv.writer(data)
    writer.writerow(["First Name", "Last Name", column])
    writer.writerow(["Example", "Person", contact])
    c["payload"]["files"][0]["data"] = base64.b64encode(data.getvalue().encode()).decode()
    c["payload_sha256"] = hashlib.sha256(captures.encode(c["payload"])).hexdigest()
    original = copy.deepcopy(c)
    result = replay.replay_capture(c)
    assert result["status"] == "invalid" and "records" not in result
    assert contact not in json.dumps(result)
    assert c == original
