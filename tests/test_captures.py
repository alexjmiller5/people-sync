import copy
import csv
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor

import pytest


def example_capture():
    from people_sync import captures

    return captures.build_capture(
        "spotify",
        "profile",
        {"eval": {"name": "Example", "path": "/user/example"}, "captured": []},
        record_id="spotify:example",
        captured_at="2026-01-01T00:00:00.000Z",
        completeness="extracted-only",
    )


def test_payload_hash_is_canonical_and_capture_ids_are_unique():
    from people_sync import captures

    a = captures.build_capture("spotify", "profile", {"eval": {}, "captured": []})
    b = captures.build_capture("spotify", "profile", {"captured": [], "eval": {}})
    assert a["payload_sha256"] == b["payload_sha256"]
    assert a["capture_id"] != b["capture_id"]
    assert captures.validate(a) == a
    assert a["payload_sha256"] == hashlib.sha256(b'{"captured":[],"eval":{}}').hexdigest()


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", 2),
        ("schema_version", True),
        ("source", "../../escape"),
        ("kind", "unknown"),
        ("capture_id", "../escape"),
        ("captured_at", "2026-02-30T00:00:00.000Z"),
        ("captured_at", "2026-01-01T00:00:00Z"),
        ("collector_fingerprint", "bad"),
        ("record_id", 1),
        ("completeness", "unknown"),
        ("exclusions", "none"),
        ("payload", []),
        ("payload_sha256", "0" * 64),
    ],
)
def test_validation_rejects_invalid_metadata_and_tampering(field, value):
    from people_sync import captures

    c = example_capture()
    c[field] = value
    with pytest.raises(ValueError):
        captures.validate(c)


def test_build_detaches_payload_and_rejects_non_json_values():
    from people_sync import captures

    payload = {"eval": {"name": "Example"}, "captured": []}
    c = captures.build_capture("spotify", "profile", payload)
    payload["eval"]["name"] = "Changed"
    assert c["payload"]["eval"]["name"] == "Example"
    with pytest.raises(ValueError):
        captures.build_capture("spotify", "profile", {"eval": float("nan"), "captured": []})


def test_fingerprint_hashes_installed_source_without_git(monkeypatch, tmp_path):
    from people_sync import captures
    import subprocess

    def forbidden(*args, **kwargs):
        raise AssertionError("fingerprint invoked a process")

    monkeypatch.setattr(subprocess, "run", forbidden)
    root = tmp_path / "package"
    root.mkdir()
    entry = root / "captures.py"
    entry.write_text("first\n")
    monkeypatch.setattr(captures, "__file__", str(entry))
    a = captures.code_fingerprint()
    assert len(a) == 64 and a == captures.code_fingerprint()
    entry.write_text("second\n")
    assert a != captures.code_fingerprint()


@pytest.fixture
def storage(monkeypatch):
    from people_sync import photos

    stored = {}
    monkeypatch.setattr(photos, "put_object", lambda key, data, **kw: stored.__setitem__(key, data))
    monkeypatch.setattr(photos, "get_object", stored.__getitem__)
    return stored


def test_retain_verifies_full_bytes_then_indexes_parallel_observations(storage, tmp_path):
    from people_sync import captures

    observations = [example_capture() for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        keys = list(pool.map(lambda c: captures.retain(c, state_dir=tmp_path), observations))
    assert len(set(keys)) == 12
    local = sorted((tmp_path / "captures").glob("*.json"))
    assert len(local) == 12
    for c, key in zip(observations, keys):
        assert key == f"profiles/spotify/captures/{c['capture_id']}.json"
        path = tmp_path / "captures" / f"{c['capture_id']}.json"
        assert path.read_bytes() == storage[key]
        assert json.loads(path.read_bytes()) == c
        assert path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "captures").stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("failure", ["upload", "read", "payload", "metadata", "index"])
def test_retention_failure_is_private_and_never_indexes(storage, monkeypatch, tmp_path, failure):
    from people_sync import captures, photos

    secret = "Bearer synthetic-secret"

    def broken(*args, **kwargs):
        raise OSError(secret)

    if failure in ("upload", "read"):
        monkeypatch.setattr(photos, "put_object" if failure == "upload" else "get_object", broken)
    elif failure in ("payload", "metadata"):

        def changed(key):
            data = json.loads(storage[key])
            if failure == "payload":
                data["payload"]["eval"]["name"] = "Other"
            else:
                data["record_id"] = "spotify:other"
            return json.dumps(data).encode()

        monkeypatch.setattr(photos, "get_object", changed)
    else:
        monkeypatch.setattr(captures.os, "replace", broken)
    with pytest.raises(photos.ArchiveError) as exc:
        captures.retain(example_capture(), state_dir=tmp_path)
    assert secret not in str(exc.value)
    assert exc.value.__suppress_context__
    assert not list(tmp_path.rglob("*.json"))


def test_private_paths_use_xdg_and_refuse_repository_or_symlink(storage, monkeypatch, tmp_path):
    from people_sync import captures, photos

    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
    c = example_capture()
    captures.retain(c)
    assert (tmp_path / "xdg" / "people-sync" / "captures" / f"{c['capture_id']}.json").exists()
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".git").mkdir()
    with pytest.raises(photos.ArchiveError):
        captures.retain(example_capture(), state_dir=repo / "state")
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    with pytest.raises(photos.ArchiveError):
        captures.retain(example_capture(), state_dir=link)


def test_invalid_capture_is_not_uploaded(storage, tmp_path):
    from people_sync import captures, photos

    c = copy.deepcopy(example_capture())
    c["payload"]["eval"]["name"] = "Tampered"
    with pytest.raises(photos.ArchiveError):
        captures.retain(c, state_dir=tmp_path)
    assert not storage


def test_validation_is_active_with_python_optimization():
    import os
    import subprocess
    import sys

    run = subprocess.run(
        [
            sys.executable,
            "-O",
            "-c",
            """
from people_sync import captures
c = captures.build_capture('spotify', 'profile', {'eval': {}, 'captured': []})
c['payload']['eval']['name'] = 'Changed'
try:
    captures.validate(c)
except ValueError:
    pass
else:
    raise RuntimeError('optimized Python accepted a tampered capture')
""",
        ],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": "src"},
    )
    assert run.returncode == 0, run.stderr


def test_export_privacy_filter_preserves_safe_bytes_and_filters_nested_fields(tmp_path):
    import base64
    from people_sync import captures, replay

    safe = b' {"friends_v2": [{"name": "Example", "timestamp": 1}]}\n'
    path = tmp_path / "friends.json"
    path.write_bytes(safe)
    c = captures.capture_export("facebook", path)
    assert c["completeness"] == "privacy-filtered"
    assert base64.b64decode(c["payload"]["files"][0]["data"]) == safe
    assert c["payload"]["files"][0]["verbatim"] is True
    (tmp_path / "followers.json").write_text(
        json.dumps(
            [
                {
                    "string_list_data": [
                        {
                            "value": "example",
                            "href": "https://instagram.com/example",
                            "timestamp": 1,
                            "email": "synthetic@example.invalid",
                        }
                    ],
                    "password": "synthetic-secret",
                }
            ]
        )
    )
    (tmp_path / "following.json").write_text('{"relationships_following":[]}')
    c = captures.capture_export("instagram", tmp_path)
    retained = base64.b64decode(c["payload"]["files"][0]["data"])
    assert b"synthetic-secret" not in retained and b"synthetic@example.invalid" not in retained
    assert replay.replay_capture(c)["records"][0]["source_id"] == "example"


@pytest.mark.parametrize("source", ["venmo", "google_contacts", "apple_contacts"])
def test_privacy_filtered_class_supports_collection_boundaries(source):
    from people_sync import captures
    from people_sync.sources import CONTACT_POLICY

    google = source == "google_contacts"
    payload = (
        {"eval": {}, "captured": []}
        if source == "venmo"
        else {
            "format": ("google" if google else "apple") + "-contacts-v1",
            "complete": True,
            "pages" if google else "databases": [
                {"ordinal": 0, "status": "complete", "entries": []}
                | ({"has_next": False} if google else {})
            ],
        }
    )
    capture = captures.build_capture(
        source,
        "profile" if source == "venmo" else "contacts",
        payload,
        completeness="privacy-filtered",
        exclusions=["source-allowlist-v1: contact details excluded"]
        if source == "venmo"
        else [CONTACT_POLICY],
    )
    assert captures.validate(capture)["completeness"] == "privacy-filtered"


@pytest.mark.parametrize("column", ["Company", "Position", "URL"])
@pytest.mark.parametrize(
    "contact",
    [
        "contact synthetic@example.invalid",
        "contact synthetic%2540example.invalid",
        "contact synthetic&#64;example.invalid",
        "contact synthetic [at] example.invalid",
        "call +1 (202) 555-0148",
        "call ＋１ (２０２) ５５５-０１４８",
        "office 123 Example Street",
        "office 221B Example Road",
        "office 123, Example Street",
        "office 123:Example Street",
        "office 123%252C%2520Example Street",
        "office 123，Example Street",
    ],
)
def test_export_rejects_contact_values_without_rewriting_original(tmp_path, column, contact):
    from urllib.parse import quote
    from people_sync import captures

    path = tmp_path / "Connections.csv"
    value = (
        "https://linkedin.com/in/example?contact=" + quote(quote(contact, safe=""), safe="")
        if column == "URL"
        else contact
    )
    row = {
        "First Name": "Example",
        "Last Name": "Person",
        "URL": "https://linkedin.com/in/example",
        "Company": "Example Co",
        "Position": "Engineer",
        "Connected On": "01 Jan 2026",
    }
    row[column] = value
    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(row))
        writer.writeheader()
        writer.writerow(row)
    original = path.read_bytes()
    with pytest.raises(ValueError) as exc:
        captures.capture_export("linkedin", path)
    assert value not in str(exc.value) and contact not in str(exc.value)
    assert path.read_bytes() == original


@pytest.mark.parametrize("column", ["Company", "Position", "URL"])
@pytest.mark.parametrize(
    "url",
    [
        "https://linkedin.com/in/example%20?access_token=synthetic-secret",
        "https://linkedin.com/in/example%2520%253Faccess_token=synthetic-secret",
        "https://linkedin.com/in/example%09?access_token=synthetic-secret",
        "https://linkedin.com/in/example&#32;?access_token=synthetic-secret",
        "https://linkedin.com/in/example%20#synthetic-secret",
        "https://linkedin.com/in/example ?access_token=synthetic-secret",
    ],
)
def test_export_checks_whole_urls_before_decoding(tmp_path, column, url):
    from people_sync import captures

    path = tmp_path / "Connections.csv"
    with path.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(["First Name", "Last Name", column])
        writer.writerow(["Example", "Person", url])
    original = path.read_bytes()
    with pytest.raises(ValueError, match="^invalid capture envelope or payload checksum$"):
        captures.capture_export("linkedin", path)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "source,entry",
    [
        ("facebook", {"name": "Example synthetic@example.invalid", "timestamp": 1700000000}),
        ("snapchat", {"Username": "example", "Display Name": "Call 202-555-0148"}),
        ("snapchat", {"Username": "example", "Display Name": 2025550148}),
        (
            "instagram",
            {
                "string_list_data": [
                    {
                        "value": "example",
                        "timestamp": 1700000000,
                        "href": "https://instagram.com/example?address=123%20Example%20Street",
                    }
                ]
            },
        ),
    ],
)
def test_export_privacy_boundary_covers_json_values_and_urls(tmp_path, source, entry):
    from people_sync import captures

    if source == "instagram":
        path = tmp_path
        (path / "followers.json").write_text(json.dumps([entry]))
        (path / "following.json").write_text('{"relationships_following":[]}')
    else:
        path = tmp_path / "friends.json"
        path.write_text(json.dumps({"friends_v2" if source == "facebook" else "Friends": [entry]}))
    with pytest.raises(ValueError):
        captures.capture_export(source, path)


def test_retain_rechecks_export_privacy_before_upload(storage, tmp_path):
    import base64
    from people_sync import captures, photos

    path = tmp_path / "friends.json"
    path.write_text('{"friends_v2":[{"name":"Example","timestamp":1700000000}]}')
    capture = captures.capture_export("facebook", path)
    unsafe = b'{"friends_v2":[{"name":"Example synthetic@example.invalid","timestamp":1700000000}]}'
    capture["payload"]["files"][0]["data"] = base64.b64encode(unsafe).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    with pytest.raises(photos.ArchiveError):
        captures.retain(capture, state_dir=tmp_path / "state")
    assert not storage and not (tmp_path / "state").exists()


@pytest.mark.parametrize("stage", ["capture", "retain"])
def test_format_normalized_url_cannot_cross_export_boundary(storage, tmp_path, capsys, stage):
    import base64
    from people_sync import captures, photos

    path = tmp_path / "Connections.csv"
    safe = (
        "First Name,Last Name,URL,Position\n"
        "Example,Person,https://linkedin.com/in/example,Engineer\n"
    ).encode()
    path.write_bytes(safe)
    capture = captures.capture_export("linkedin", path)
    unsafe = safe.replace(
        b"Engineer", "https:\u200b//linkedin.com/in/example?access_token=synthetic-secret".encode()
    )
    path.write_bytes(unsafe)
    capture["payload"]["files"][0]["data"] = base64.b64encode(unsafe).decode()
    capture["payload_sha256"] = hashlib.sha256(captures.encode(capture["payload"])).hexdigest()
    original = copy.deepcopy(capture)
    if stage == "capture":
        with pytest.raises(ValueError, match="^invalid capture envelope or payload checksum$"):
            captures.capture_export("linkedin", path)
    else:
        with pytest.raises(photos.ArchiveError, match="^raw archive failed$"):
            captures.retain(capture, state_dir=tmp_path / "state")
    assert path.read_bytes() == unsafe and capture == original
    assert not storage and not (tmp_path / "state").exists()
    output = capsys.readouterr()
    assert "synthetic-secret" not in output.out + output.err
