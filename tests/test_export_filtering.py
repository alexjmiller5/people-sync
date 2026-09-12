"""Synthetic export privacy regressions, through capture and offline replay."""

import base64
import copy
import hashlib
import json

import pytest

from people_sync import captures, replay


def retained(capture, index=0):
    return base64.b64decode(capture["payload"]["files"][index]["data"])


def test_linkedin_filters_fields_without_losing_records_or_ordinals(tmp_path):
    path = tmp_path / "Connections.csv"
    original = (
        "First Name,Last Name,URL,Company,Position,Connected On\n"
        "Example,Person,https://www.linkedin.com/in/example-123456789/,secret@example.test,Engineer,01 Jan 2026\n"
        "\n"
        "Second,Person,https://linkedin.com/in/second,Studio,3D Designer,02 Jan 2026\n"
    ).encode()
    path.write_bytes(original)
    with pytest.raises(ValueError):
        captures._check_export_value("https://www.linkedin.com/in/example-123456789/")
    capture = captures.capture_export("linkedin", path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == ["example-123456789", "second"]
    assert [(o["ordinal"], o["record_ids"]) for o in result["observations"]] == [
        (0, ["linkedin:example-123456789"]),
        (1, []),
        (2, ["linkedin:second"]),
    ]
    assert result["records"][0]["raw"]["Company"] == ""
    assert result["records"][0]["raw"]["Position"] == "Engineer"
    assert result["records"][1]["raw"]["Company"] == "Studio"
    manifest = capture["payload"]["field_exclusions"]
    assert manifest == {
        "version": 1,
        "entries": [
            {
                "role": "export",
                "ordinal": 0,
                "path": ["Company"],
                "reason": "unsafe-or-ambiguous-value",
            },
            {
                "role": "export",
                "ordinal": 2,
                "path": ["Position"],
                "reason": "unsafe-or-ambiguous-value",
            },
        ],
    }
    assert result["field_exclusions"] == manifest
    assert any("missing" in limitation for limitation in result["limitations"])
    assert replay.replay_capture(capture) == result
    assert b"secret@example.test" not in retained(capture) + captures.encode(capture)
    assert path.read_bytes() == original


@pytest.mark.parametrize("handle", ["example_123_name", "123456789", "example_123456789"])
def test_instagram_typed_handles_survive_with_malformed_ordinals(tmp_path, handle):
    entry = {
        "string_list_data": [
            {
                "value": handle,
                "href": f"https://www.instagram.com/{handle}/",
                "timestamp": 1700000000,
            }
        ]
    }
    original = captures.encode([entry, {}, {"string_list_data": []}])
    (tmp_path / "followers.json").write_bytes(original)
    (tmp_path / "following.json").write_text('{"relationships_following":[]}')
    with pytest.raises(ValueError):
        captures._check_export_value(handle)
    capture = captures.capture_export("instagram", tmp_path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == [handle]
    assert [(o["ordinal"], o["status"]) for o in result["observations"]] == [
        (0, "parsed"),
        (1, "skipped"),
        (2, "skipped"),
    ]
    assert (tmp_path / "followers.json").read_bytes() == original


@pytest.mark.parametrize(
    "value",
    [
        "https://user:secret@linkedin.com/in/example-123456789/",
        "https://linkedin.com/in/example-123456789/?secret=value",
        "https://linkedin.com/in/example-123456789/#secret",
        "https://foreign.test/in/example-123456789/",
        "https://foreign.test/in/example/",
        "https://linkedin.com/other/example-123456789/",
        "https://linkedin.com/in/example%2540example.test/",
    ],
)
def test_invalid_identity_is_excluded_but_forged_retained_input_is_rejected(tmp_path, value):
    path = tmp_path / "Connections.csv"
    header = "First Name,Last Name,URL,Position\n"
    path.write_text(header + f"Example,Person,{value},Engineer\n")
    capture = captures.capture_export("linkedin", path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok" and result["records"] == []
    assert result["observations"][0]["ordinal"] == 0
    assert result["field_exclusions"]["entries"] == [
        {"role": "export", "ordinal": 0, "path": ["URL"], "reason": "unsafe-or-ambiguous-value"}
    ]
    forged = copy.deepcopy(capture)
    forged["payload"]["files"][0]["data"] = base64.b64encode(path.read_bytes()).decode()
    forged["payload_sha256"] = hashlib.sha256(captures.encode(forged["payload"])).hexdigest()
    assert replay.replay_capture(forged)["status"] == "invalid"
    with pytest.raises(ValueError):
        captures.validate(forged)


@pytest.mark.parametrize(
    "value",
    [
        "123456789",
        "3D Designer",
        "password: synthetic-secret",
        "card 4111111111111111",
        "https://foreign.test/123456789",
    ],
)
def test_generic_privacy_remains_strict_and_filters_only_source_fields(tmp_path, value):
    path = tmp_path / "friends.json"
    path.write_text(
        json.dumps(
            {
                "friends_v2": [
                    {"name": value, "timestamp": 1700000000},
                    {"name": "Example Person"},
                    {},
                ]
            }
        )
    )
    capture = captures.capture_export("facebook", path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == ["example_person"]
    assert [o["ordinal"] for o in result["observations"]] == [0, 1, 2]
    assert value.encode() not in retained(capture)


@pytest.mark.parametrize(
    "handle", ["example/", "example?secret=value", "example#secret", "example%2540example.test"]
)
def test_invalid_instagram_handle_is_missing_without_losing_other_rows(tmp_path, handle):
    (tmp_path / "followers.json").write_text(
        json.dumps(
            [
                {"string_list_data": [{"value": handle}]},
                {"string_list_data": [{"value": "example_safe"}]},
            ]
        )
    )
    (tmp_path / "following.json").write_text('{"relationships_following":[]}')
    capture = captures.capture_export("instagram", tmp_path)
    result = replay.replay_capture(capture)
    assert [r["source_id"] for r in result["records"]] == ["example_safe"]
    assert [o["ordinal"] for o in result["observations"]] == [0, 1]
    assert result["field_exclusions"]["entries"][0]["path"] == ["string_list_data", 0, "value"]


@pytest.mark.parametrize(
    "value",
    ["password: synthetic-secret", "password%3A%20synthetic-secret", "bearer synthetic-secret"],
)
def test_credentials_in_timestamp_are_excluded_and_rejected_at_boundary(tmp_path, value):
    path = tmp_path / "friends.json"
    original = captures.encode({"friends_v2": [{"name": "Example Person", "timestamp": value}]})
    path.write_bytes(original)
    capture = captures.capture_export("facebook", path)
    result = replay.replay_capture(capture)
    assert result["records"][0]["raw"]["timestamp"] is None
    assert result["field_exclusions"]["entries"][0]["path"] == ["timestamp"]
    capture["payload"]["files"][0]["data"] = base64.b64encode(original).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_structural_exclusions_have_safe_paths_and_keep_malformed_rows(tmp_path):
    path = tmp_path / "friends.json"
    original = captures.encode(
        {
            "friends_v2": [
                {"name": "Example Person", "secret@example.test": "synthetic-secret"},
                "malformed",
                {"name": ["malformed"]},
                {},
                {"name": "Second Person"},
            ],
            "secret@example.test": "synthetic-secret",
        }
    )
    path.write_bytes(original)
    capture = captures.capture_export("facebook", path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == ["example_person", "second_person"]
    assert [o["ordinal"] for o in result["observations"]] == [0, 1, 2, 3, 4]
    entries = result["field_exclusions"]["entries"]
    assert [(e["ordinal"], e["path"]) for e in entries] == [
        (None, ["field", 1]),
        (0, ["field", 1]),
        (1, []),
        (2, ["name"]),
    ]
    assert b"secret@example.test" not in captures.encode(capture) + retained(capture)
    assert b"synthetic-secret" not in captures.encode(capture) + retained(capture)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "source,body",
    [
        ("facebook", "{}"),
        ("facebook", '{"friends_v2":{}}'),
        ("linkedin", "unknown,header\n"),
        ("instagram", '{"unknown":[]}'),
    ],
)
def test_unknown_shape_fails_explicitly(tmp_path, source, body):
    path = tmp_path / "export.json"
    path.write_text(body)
    if source == "instagram":
        (tmp_path / "followers.json").write_text(body)
        (tmp_path / "following.json").write_text('{"relationships_following":[]}')
        path = tmp_path
    with pytest.raises((ValueError, KeyError, StopIteration)):
        captures.capture_export(source, path)


@pytest.mark.parametrize(
    "change",
    [
        {"reason": "secret@example.test"},
        {"path": ["secret@example.test"]},
        {"ordinal": 8},
        {"role": "unknown"},
        {"excluded_value": "synthetic-secret"},
    ],
)
def test_checksum_valid_manifest_tampering_is_rejected(tmp_path, change):
    path = tmp_path / "friends.json"
    path.write_text('{"friends_v2":[{"name":"secret@example.test"}]}')
    capture = captures.capture_export("facebook", path)
    capture["payload"]["field_exclusions"]["entries"][0].update(change)
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_filtered_ingest_retains_and_verifies_before_parsing(tmp_path, monkeypatch, capsys):
    from people_sync import cli, ledger, parsers, photos

    path = tmp_path / "Connections.csv"
    path.write_text(
        "First Name,Last Name,URL,Company\nExample,Person,https://linkedin.com/in/example-123456789,secret@example.test\n"
    )
    original = path.read_bytes()
    stored, events, written = {}, [], []
    parse = parsers.parse_linkedin

    def put(key, body, **kwargs):
        events.append("retain")
        stored[key] = body

    def get(key):
        events.append("verify")
        return stored[key]

    def checked_parse(path):
        assert events == ["retain", "verify"]
        events.append("parse")
        return parse(path)

    monkeypatch.setattr(photos, "put_object", put)
    monkeypatch.setattr(photos, "get_object", get)
    monkeypatch.setattr(parsers, "parse_linkedin", checked_parse)
    monkeypatch.setattr(ledger, "upsert", lambda rows: written.extend(rows) or {"new": len(rows)})
    cli.main(["ingest", "linkedin", "--path", str(path)])
    assert json.loads(capsys.readouterr().out) == {"new": 1}
    assert [r.source_id for r in written] == ["example-123456789"]
    assert written[0].raw["Company"] == ""
    assert written[0].capture_key in stored
    assert events == ["retain", "verify", "parse"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "body",
    [
        '{"friends_v2":[{"name":"secret@example.test","name":"Example Person"}]}',
        '{"friends_v2":[{"name":"First Person"}],"friends_v2":[{"name":"Second Person"}]}',
    ],
)
def test_duplicate_json_fields_fail_without_silently_losing_values(tmp_path, body):
    path = tmp_path / "friends.json"
    path.write_text(body)
    with pytest.raises(ValueError):
        captures.capture_export("facebook", path)
    assert path.read_text() == body


@pytest.mark.parametrize(
    "source,body",
    [
        ("facebook", {"friends_v2": [{"name": 42}, {"name": "Example Person"}]}),
        ("snapchat", {"Friends": [{"Username": True}, {"Username": "example"}]}),
    ],
)
def test_malformed_scalar_identity_does_not_prevent_other_records(tmp_path, source, body):
    path = tmp_path / "friends.json"
    path.write_text(json.dumps(body))
    result = replay.replay_capture(captures.capture_export(source, path))
    assert result["status"] == "ok"
    assert len(result["records"]) == 1
    assert [(o["ordinal"], o["status"]) for o in result["observations"]] == [
        (0, "skipped"),
        (1, "parsed"),
    ]


@pytest.mark.parametrize(
    "url",
    [
        "https://user:secret@instagram.com/example/",
        "https://instagram.com/example/?secret=value",
        "https://instagram.com/example/#secret",
        "https://foreign.test/example/",
        "https://instagram.com/example%2540example.test/",
    ],
)
def test_instagram_url_boundary_filters_and_rejects_forgery(tmp_path, url):
    original = captures.encode([{"string_list_data": [{"value": "example", "href": url}]}])
    (tmp_path / "followers.json").write_bytes(original)
    (tmp_path / "following.json").write_text('{"relationships_following":[]}')
    capture = captures.capture_export("instagram", tmp_path)
    result = replay.replay_capture(capture)
    assert result["records"][0]["source_id"] == "example"
    assert result["field_exclusions"]["entries"][0]["path"] == ["string_list_data", 0, "href"]
    capture["payload"]["files"][0]["data"] = base64.b64encode(original).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_filtered_envelope_cannot_reintroduce_disallowed_fields(tmp_path):
    path = tmp_path / "friends.json"
    path.write_text('{"friends_v2":[{"name":"Example Person"}]}')
    capture = captures.capture_export("facebook", path)
    unsafe = b'{"friends_v2":[{"name":"Example Person","token":"synthetic-secret"}]}'
    capture["payload"]["files"][0]["data"] = base64.b64encode(unsafe).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_instagram_structural_placeholders_remain_replayable(tmp_path):
    (tmp_path / "followers.json").write_text(
        '[null,{"string_list_data":42},{"string_list_data":[null]},{"string_list_data":[{"value":"example"}]}]'
    )
    (tmp_path / "following.json").write_text('{"relationships_following":[]}')
    capture = captures.capture_export("instagram", tmp_path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok"
    assert [r["source_id"] for r in result["records"]] == ["example"]
    assert [(o["ordinal"], o["status"]) for o in result["observations"]] == [
        (0, "skipped"),
        (1, "skipped"),
        (2, "skipped"),
        (3, "parsed"),
    ]


@pytest.mark.parametrize("source", ["facebook", "instagram"])
@pytest.mark.parametrize(
    "epoch",
    [
        True,
        False,
        -1,
        4102444800,
        4111111111111111,
        1700000000.0,
        float("nan"),
        float("inf"),
        float("-inf"),
        "1700000000",
        "",
    ],
)
def test_invalid_epoch_is_excluded_and_checksum_valid_forgery_rejected(tmp_path, source, epoch):
    if source == "facebook":
        body = {"friends_v2": [{"name": "Example Person", "timestamp": epoch}]}
        path = tmp_path / "friends.json"
        original_path = path
    else:
        body = [{"string_list_data": [{"value": "example_123456789", "timestamp": epoch}]}]
        path = tmp_path
        original_path = path / "followers.json"
        (path / "following.json").write_text('{"relationships_following":[]}')
    original = json.dumps(body).encode()
    original_path.write_bytes(original)
    capture = captures.capture_export(source, path)
    result = replay.replay_capture(capture)
    assert result["status"] == "ok" and len(result["records"]) == 1
    raw = result["records"][0]["raw"]
    entry = raw if source == "facebook" else raw["followers"]["string_list_data"][0]
    assert entry["timestamp"] is None
    assert result["field_exclusions"]["entries"][0]["reason"] == "unsafe-or-ambiguous-value"
    assert original_path.read_bytes() == original
    capture["payload"]["files"][0]["data"] = base64.b64encode(original).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"
    with pytest.raises(ValueError):
        captures._check_export_value(epoch, "timestamp")


@pytest.mark.parametrize("epoch", [0, 1, 1700000000, 4102444799])
def test_supported_integer_epochs_survive_unchanged(tmp_path, epoch):
    path = tmp_path / "friends.json"
    original = captures.encode({"friends_v2": [{"name": "Example Person", "timestamp": epoch}]})
    path.write_bytes(original)
    capture = captures.capture_export("facebook", path)
    result = replay.replay_capture(capture)
    assert result["records"][0]["raw"]["timestamp"] == epoch
    assert result["field_exclusions"]["entries"] == []
    assert retained(capture) == original


def test_unicode_linkedin_slugs_preserve_parser_ids_and_every_named_row(tmp_path):
    path = tmp_path / "Connections.csv"
    original = (
        "First Name,Last Name,URL,Company\n"
        "Example,One,https://www.linkedin.com/in/exampl%C3%A9-123456789/,Studio\n"
        "Example,Two,https://linkedin.com/in/%E6%B5%8B%E8%AF%95-987654321,Studio\n"
        "Example,Three,https://linkedin.com/in/example-%E2%80%9Cnickname%E2%80%9D-123456789/,Studio\n"
        ",,,\n"
    ).encode()
    path.write_bytes(original)
    capture = captures.capture_export("linkedin", path)
    result = replay.replay_capture(capture)
    expected = [
        "exampl%c3%a9-123456789",
        "%e6%b5%8b%e8%af%95-987654321",
        "example-%e2%80%9cnickname%e2%80%9d-123456789",
    ]
    assert [r["source_id"] for r in result["records"]] == expected
    assert [r["handle"] for r in result["records"]] == expected
    assert [o["record_ids"] for o in result["observations"]] == [
        ["linkedin:" + s] for s in expected
    ] + [[]]
    assert [o["ordinal"] for o in result["observations"]] == [0, 1, 2, 3]
    assert result["field_exclusions"]["entries"] == []
    assert retained(capture) == path.read_bytes() == original
    assert replay.replay_capture(capture) == result


@pytest.mark.parametrize(
    "slug",
    [
        "example%2Fother-123456789",
        "example%252Fother-123456789",
        "example%40example.test",
        "tel%3A123456789",
        "example%00-123456789",
        "example%5Cother",
        "example%3Fsecret",
        "example%23secret",
        "example%20other",
        "example%EF%BC%8Fother",
        "example%E2%80%8Bother",
        "%FF",
        "%C0%AF",
        "password-synthetic-123456789",
        "pass%77ord-synthetic-123456789",
        "example%EF%BC%A0example.test",
        "example%26%2364%3Bexample.test",
    ],
)
def test_unicode_linkedin_boundary_still_rejects_encoded_forbidden_segments(tmp_path, slug):
    path = tmp_path / "Connections.csv"
    original = (
        f"First Name,Last Name,URL\nExample,Person,https://www.linkedin.com/in/{slug}/\n".encode()
    )
    path.write_bytes(original)
    capture = captures.capture_export("linkedin", path)
    result = replay.replay_capture(capture)
    assert result["records"] == []
    assert result["field_exclusions"]["entries"][0]["path"] == ["URL"]
    capture["payload"]["files"][0]["data"] = base64.b64encode(original).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    assert replay.replay_capture(capture)["status"] == "invalid"


def test_shared_linkedin_identity_preserves_existing_ascii_alphabet():
    from people_sync.scrape import snapshot

    assert snapshot._identity("example.name_123-456", "linkedin") == "example.name_123-456"
    for value in (".", "..", "%2E%2E"):
        with pytest.raises(ValueError):
            snapshot._identity(value, "linkedin")
