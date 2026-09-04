import base64
import hashlib
import json
import sqlite3

from contact_sync.scrape import run
from contact_sync.scrape.profile import Profile


class FakeModule:
    """Stands in for a platform module (instagram.py's shape)."""

    URL = "https://example.test/{handle}/"
    CAPTURE = [r"web_profile_info"]
    EXTRACTOR_JS = "EXTRACT()"

    @staticmethod
    def parse(eval_result, captured):
        return Profile(
            platform="testplatform",
            profile_url=f"https://example.test/{eval_result.get('username')}/",
            platform_id=eval_result.get("username"),
            display_name=eval_result.get("full_name"),
            avatar_url="https://example.test/avatar.jpg",
        )


class FakeBrowser:
    def __init__(self, page_text="Ordinary Title\nOrdinary bio text"):
        self.page_text = page_text
        self.navigated: list[str] = []
        self.closed = False

    def navigate(self, url, wait_ms, capture=None):
        self.navigated.append(url)
        return {"captured": [], "load_ms": 10.0}

    def eval(self, js):
        if "innerText" in js:
            return self.page_text
        if "fetch(" in js:
            return None
        return json.dumps({"username": "u1", "full_name": "Test User"})

    def close(self):
        self.closed = True


def _record(**overrides) -> dict:
    row = {"id": "testplatform:u1", "handle": "u1", "avatar_r2_key": None, "avatar_sha256": None}
    row.update(overrides)
    return row


def _patch_common(mocker, records, browser=None, allow=True):
    mocker.patch("contact_sync.scrape.run._select_records", return_value=records)
    mocker.patch("contact_sync.scrape.run.import_module", return_value=FakeModule)
    browser = browser or FakeBrowser()
    mocker.patch("contact_sync.scrape.run.Browser.connect", return_value=browser)
    pacer_cls = mocker.patch("contact_sync.scrape.run.Pacer")
    pacer = pacer_cls.return_value
    pacer.allow.return_value = allow
    pacer.next_gap.return_value = 0.0
    mocker.patch("contact_sync.scrape.run.time.sleep")
    return browser, pacer


def test_cap_reached_stops_before_navigating(mocker):
    browser, pacer = _patch_common(mocker, [_record()], allow=False)
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 0, "halted": "daily cap reached"}
    assert browser.navigated == []
    assert browser.closed is True
    put_object.assert_not_called()
    upsert.assert_not_called()
    pacer.record.assert_not_called()


def test_challenge_page_halts_and_writes_nothing(mocker):
    browser, pacer = _patch_common(
        mocker, [_record()], browser=FakeBrowser(page_text="Please log in to continue")
    )
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 0, "halted": "challenge page"}
    assert browser.navigated == ["https://example.test/u1/"]
    put_object.assert_not_called()
    upsert.assert_not_called()
    pacer.record.assert_not_called()
    assert browser.closed is True


def test_normal_record_uploads_raw_before_upsert_and_calls_pace(mocker):
    browser, pacer = _patch_common(mocker, [_record()])
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=b"avatar-bytes")
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    manager = mocker.MagicMock()
    manager.attach_mock(put_object, "put_object")
    manager.attach_mock(upsert, "upsert_profile")

    result = run.scrape("testplatform")

    assert result == {"done": 1, "skipped": 0, "halted": None}

    call_names = [c[0] for c in manager.mock_calls]
    assert call_names.index("put_object") < call_names.index("upsert_profile")

    raw_calls = [c for c in put_object.call_args_list if c.args[0].startswith("profiles/")]
    assert len(raw_calls) == 1
    assert raw_calls[0].args[0].startswith("profiles/testplatform/testplatform_u1/")
    payload = json.loads(raw_calls[0].args[1])
    assert payload["eval"]["username"] == "u1"

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    sha = hashlib.sha256(b"avatar-bytes").hexdigest()
    assert avatar_calls[0].args[0] == f"photos/records/testplatform/testplatform_u1-{sha[:8]}.jpg"

    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert profile.record_id == "testplatform:u1"
    assert avatar_key == avatar_calls[0].args[0]
    assert avatar_sha == sha
    assert raw_key == raw_calls[0].args[0]

    pacer.record.assert_called_once()
    pacer.next_gap.assert_called_once()


def test_avatar_dedupe_skips_reupload_when_sha_matches(mocker):
    sha = hashlib.sha256(b"avatar-bytes").hexdigest()
    record = _record(
        avatar_r2_key="photos/records/testplatform/testplatform_u1-existing.jpg",
        avatar_sha256=sha,
    )
    browser, pacer = _patch_common(mocker, [record])
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=b"avatar-bytes")
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert avatar_calls == []  # no re-upload, bytes are identical

    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert avatar_key == "photos/records/testplatform/testplatform_u1-existing.jpg"
    assert avatar_sha == sha


def test_avatar_upload_happens_when_sha_changes(mocker):
    record = _record(
        avatar_r2_key="photos/records/testplatform/testplatform_u1-old.jpg",
        avatar_sha256="a-different-sha",
    )
    browser, pacer = _patch_common(mocker, [record])
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=b"new-avatar-bytes")
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    profile, avatar_key, avatar_sha, raw_key = upsert.call_args.args
    assert avatar_key == avatar_calls[0].args[0]
    assert avatar_sha == hashlib.sha256(b"new-avatar-bytes").hexdigest()


def test_avatar_falls_back_to_page_fetch_when_direct_fetch_fails(mocker):
    _patch_common(mocker, [_record()])
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=None)
    b64 = base64.b64encode(b"page-fetched-bytes").decode()

    class PageFetchBrowser(FakeBrowser):
        def eval(self, js):
            if "innerText" in js:
                return self.page_text
            if "fetch(" in js:
                return b64
            return json.dumps({"username": "u1", "full_name": "Test User"})

    mocker.patch("contact_sync.scrape.run.Browser.connect", return_value=PageFetchBrowser())
    put_object = mocker.patch("contact_sync.photos.put_object")
    upsert = mocker.patch("contact_sync.scrape.run.upsert_profile")

    run.scrape("testplatform")

    sha = hashlib.sha256(b"page-fetched-bytes").hexdigest()
    avatar_calls = [c for c in put_object.call_args_list if c.args[0].startswith("photos/records/")]
    assert len(avatar_calls) == 1
    assert avatar_calls[0].args[1] == b"page-fetched-bytes"
    _, avatar_key, avatar_sha, _ = upsert.call_args.args
    assert avatar_sha == sha


def test_records_with_no_handle_are_skipped_not_navigated(mocker):
    browser, pacer = _patch_common(mocker, [_record(handle=None)])
    put_object = mocker.patch("contact_sync.photos.put_object")

    result = run.scrape("testplatform")

    assert result == {"done": 0, "skipped": 1, "halted": None}
    assert browser.navigated == []
    put_object.assert_not_called()


def test_max_n_limits_records_processed(mocker):
    records = [
        {
            "id": f"testplatform:u{i}",
            "handle": f"u{i}",
            "avatar_r2_key": None,
            "avatar_sha256": None,
        }
        for i in range(3)
    ]
    browser, pacer = _patch_common(mocker, records)
    mocker.patch("contact_sync.photos.fetch_url_photo", return_value=None)
    mocker.patch("contact_sync.photos.put_object")
    mocker.patch("contact_sync.scrape.run.upsert_profile")

    result = run.scrape("testplatform", max_n=2)

    assert result["done"] == 2
    assert len(browser.navigated) == 2


def test_records_sql_filters_deleted_ignored_and_stale_window():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE contact_records "
        "(id TEXT, source TEXT, handle TEXT, name TEXT, status TEXT, "
        "deleted_at TEXT, first_seen TEXT)"
    )
    conn.execute(
        "CREATE TABLE contact_profiles (record_id TEXT, scraped_at TEXT, "
        "avatar_r2_key TEXT, avatar_sha256 TEXT)"
    )
    conn.executemany(
        "INSERT INTO contact_records VALUES (?,?,?,?,?,?,?)",
        [
            ("testplatform:new", "testplatform", "new", None, "pending", None, "3"),
            ("testplatform:stale", "testplatform", "stale", None, "matched", None, "2"),
            ("testplatform:fresh", "testplatform", "fresh", None, "pending", None, "1"),
            (
                "testplatform:deleted",
                "testplatform",
                "deleted",
                None,
                "pending",
                "2026-01-01T00:00:00.000Z",
                "4",
            ),
            ("testplatform:ignored", "testplatform", "ignored", None, "ignored", None, "5"),
            ("other:someone", "other", "someone", None, "pending", None, "6"),
        ],
    )
    conn.executemany(
        "INSERT INTO contact_profiles VALUES (?,?,?,?)",
        [
            ("testplatform:fresh", "2026-08-01T00:00:00.000Z", None, None),  # recent: excluded
            ("testplatform:stale", "2026-01-01T00:00:00.000Z", None, None),  # stale: included
        ],
    )
    conn.commit()

    query = run._records_sql("testplatform", "2026-03-01T00:00:00.000Z")
    ids = [row[0] for row in conn.execute(query).fetchall()]

    assert ids == ["testplatform:stale", "testplatform:new"]
