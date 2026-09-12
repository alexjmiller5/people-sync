import json

import pytest

from people_sync import captures, photos
from people_sync.scrape import partiful
from people_sync.scrape.profile import ExtractError

FIXTURE = {
    "name": "Test Person",
    "instagram": ["test.person", "partiful"],
    "avatar": "https://partiful.imgix.net/profileImages/abc123",
    "events": 7,
    "birthday_month": "August birthday",
    "path": "/u/uid123",
}


@pytest.fixture(autouse=True)
def no_live_archive(mocker, monkeypatch, tmp_path):
    stored = {}
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    mocker.patch(
        "people_sync.photos.put_object",
        side_effect=lambda key, body, **kw: stored.update({key: body}),
    )
    mocker.patch("people_sync.photos.get_object", side_effect=stored.__getitem__)


def test_parse_raises_on_no_profile():
    with pytest.raises(ExtractError, match="no-profile"):
        partiful.parse({"error": "no-profile"})


def test_parse_maps_profile_fields_and_keeps_instagram_handles():
    p = partiful.parse(FIXTURE)
    assert p.platform == "partiful"
    assert p.platform_id == "uid123"
    assert p.profile_url == "https://partiful.com/u/uid123"
    assert p.display_name == "Test Person"
    assert p.links == [
        "https://www.instagram.com/test.person/",
        "https://www.instagram.com/partiful/",
    ]
    assert p.raw["instagram_handles"] == ["test.person", "partiful"]
    assert p.birthday == "--08"
    assert p.mutual_count == 7
    assert p.avatar_url == "https://partiful.imgix.net/profileImages/abc123"


def test_extractor_js_drops_partifuls_own_instagram_and_parses_in_node():
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        pytest.skip("node")
    assert "partiful" in partiful.EXTRACTOR_JS
    r = subprocess.run(
        [node, "-e", "new Function(process.argv[1])", partiful.EXTRACTOR_JS],
        capture_output=True,
        text=True,
    )
    assert r.returncode == 0, r.stderr


class FakeBrowser:
    """Two rows; clicking routes to /u/<n>, back returns to the list."""

    def __init__(self):
        self.path = "/mutuals"
        self.clicked = []
        self.dialog = True

    def navigate(self, url, wait_ms=None, capture=None):
        self.path = "/mutuals"

    def click(self, selector):
        self.clicked.append(selector)
        if selector == partiful.ONBOARDING_DISMISS:
            self.dialog = False
        elif selector.startswith("#ps_row_"):
            self.path = "/u/uid" + selector.rsplit("_", 1)[-1]

    def wait_for(self, js, timeout):
        return True

    def eval(self, js):
        if "role=dialog" in js:
            return self.dialog
        if js == partiful.ROW_COUNT_JS:
            return 2
        if js.startswith("(function(i)"):
            i = int(js.rsplit("(", 1)[-1].rstrip(")"))
            return {
                "selector": f"#ps_row_{i}",
                "name": f"Person {i}",
                "last_seen": "2 days ago",
                "shared_events": 3 + i,
                "thumb": "https://x/t",
            }
        if js == partiful.EXTRACTOR_JS:
            return json.dumps({**FIXTURE, "name": "Person", "path": self.path})
        if js == "location.pathname":
            return self.path
        if js == "history.back()":
            self.path = "/mutuals"
            return None
        return None


def test_harvest_clicks_each_row_and_parses_the_profile_behind_it(mocker):
    mocker.patch("people_sync.scrape.partiful.time.sleep")
    b = FakeBrowser()

    out = list(partiful.harvest(b))

    assert b.clicked[0] == partiful.ONBOARDING_DISMISS
    assert [(i, total) for i, total, _ in out] == [(0, 2), (1, 2)]
    e0 = out[0][2]
    assert e0["uid"] == "uid0" and e0["name"] == "Person 0" and e0["shared_events"] == 3
    assert e0["profile"].raw["instagram_handles"] == ["test.person", "partiful"]
    assert b.path == "/mutuals"


def test_ingest_entry_writes_a_ledger_record_and_a_profile_row(mocker):
    upsert = mocker.patch("people_sync.ledger.upsert")
    upsert_profile = mocker.patch("people_sync.scrape.profile.upsert_profile")
    upload = photos.put_object
    mocker.patch("people_sync.lifedata.now_iso", return_value="2026-01-01T00:00:00.000Z")
    profile = partiful.parse({**FIXTURE, "future_field": "retained"})
    entry = {
        "uid": "uid123",
        "name": "Test Person",
        "last_seen": "2 days ago",
        "shared_events": 4,
        "thumb": "t",
        "profile": profile,
    }

    assert partiful.ingest_entry(entry) == "partiful:uid123"

    record = upsert.call_args.args[0][0]
    assert (
        record.source == "partiful" and record.source_id == "uid123" and record.handle == "uid123"
    )
    assert record.raw["instagram_handles"] == ["test.person", "partiful"]
    assert record.raw["url"] == "https://partiful.com/u/uid123"
    assert upsert_profile.call_args.args[0].record_id == "partiful:uid123"
    upload.assert_called_once()
    key, body = upload.call_args.args
    capture = captures.validate(json.loads(body))
    assert key == f"profiles/partiful/captures/{capture['capture_id']}.json"
    assert capture["captured_at"] == "2026-01-01T00:00:00.000Z"
    assert capture["record_id"] == "partiful:uid123"
    assert capture["payload"] == {"eval": {**FIXTURE, "future_field": "retained"}, "captured": []}
    assert upload.call_args.kwargs["content_type"] == "application/json"
    assert upsert_profile.call_args.kwargs["raw_r2_key"] == key


def test_ingest_entry_skips_rows_without_a_profile(mocker):
    upsert = mocker.patch("people_sync.ledger.upsert")
    assert partiful.ingest_entry({"name": "X", "error": "no-navigation"}) is None
    upsert.assert_not_called()


def test_ingest_upload_failure_does_not_replace_a_retained_profile(mocker):
    mocker.patch("people_sync.ledger.upsert")
    cache = mocker.patch("people_sync.scrape.profile.upsert_profile")
    mocker.patch("people_sync.photos.put_object", side_effect=RuntimeError("upload failed"))
    with pytest.raises(RuntimeError, match="raw archive failed"):
        partiful.ingest_entry({"uid": "uid123", "profile": partiful.parse(FIXTURE)})
    cache.assert_not_called()


def test_harvest_archives_before_parse_and_back_navigation(mocker):
    mocker.patch("people_sync.scrape.partiful.time.sleep")
    b = FakeBrowser()
    archived = {}

    def upload(key, body, **kwargs):
        assert b.path.startswith("/u/")
        archived[key] = body

    def broken_parser(*args):
        assert len(archived) == 1
        raise ExtractError("no-profile")

    mocker.patch("people_sync.photos.put_object", side_effect=upload)
    mocker.patch("people_sync.photos.get_object", side_effect=archived.__getitem__)
    mocker.patch.object(partiful, "parse", side_effect=broken_parser)
    entries = list(partiful.harvest(b, limit=1))

    assert len(archived) == 1
    entry = entries[0][2]
    assert entry["error"] == "no-profile"
    saved = captures.validate(json.loads(archived[entry["raw_r2_key"]]))["payload"]
    assert json.loads(saved["raw_eval"])["path"] == "/u/uid0"
    assert saved["context"]["shared_events"] == 3
    assert b.path == "/mutuals"


@pytest.mark.parametrize("failure", ["upload", "readback", "index"])
def test_harvest_archive_failure_stops_before_parse_and_back(mocker, failure):
    mocker.patch("people_sync.scrape.partiful.time.sleep")
    b = FakeBrowser()
    if failure == "readback":
        mocker.patch("people_sync.photos.get_object", return_value=b"wrong bytes")
    else:
        target = (
            "people_sync.photos.put_object"
            if failure == "upload"
            else "people_sync.captures.os.replace"
        )
        mocker.patch(target, side_effect=OSError("secret storage detail"))
    parse = mocker.patch.object(partiful, "parse")
    with pytest.raises(RuntimeError, match="raw archive failed"):
        list(partiful.harvest(b))
    parse.assert_not_called()
    assert b.path == "/u/uid0"


def test_ingest_reuses_harvest_archive(mocker):
    mocker.patch("people_sync.scrape.partiful.time.sleep")
    b = FakeBrowser()
    upload = photos.put_object
    mocker.patch("people_sync.ledger.upsert")
    cache = mocker.patch("people_sync.scrape.profile.upsert_profile")
    entry = next(partiful.harvest(b, limit=1))[2]
    partiful.ingest_entry(entry)
    upload.assert_called_once()
    assert cache.call_args.kwargs["raw_r2_key"] == entry["raw_r2_key"]


def test_harvest_retains_malformed_json_before_decoder_fails(mocker):
    mocker.patch("people_sync.scrape.partiful.time.sleep")
    b = FakeBrowser()
    original_eval = b.eval
    b.eval = lambda js: "{broken" if js == partiful.EXTRACTOR_JS else original_eval(js)
    upload = photos.put_object
    with pytest.raises(json.JSONDecodeError):
        list(partiful.harvest(b))
    assert (
        captures.validate(json.loads(upload.call_args.args[1]))["payload"]["raw_eval"] == "{broken"
    )
    assert b.path == "/u/uid0"
